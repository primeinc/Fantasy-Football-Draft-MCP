"""set_draft_queue keeps what the user queued.

The queue has two authors, the user in the ESPN app and this server, and ESPN's
protocol carries no add or remove -- `DRAFT_LIST` is the whole list. So a call
that sends only its own names silently deletes everything the user built. That
is what these tests are about.
"""
import asyncio
import json
from pathlib import Path

import pandas as pd
import pytest

from ffdraft import board, espn_live, server, watch
from ffdraft.config import LeagueSettings

# ESPN ids, and the names the fake crosswalk gives them.
USER_QUEUE = [4429795, 4362628, 3916433]
OURS = [4429205, 4429059]
NAMES = {"4429795": "Jahmyr Gibbs", "4362628": "Ja'Marr Chase", "3916433": "Cam Skattebo",
         "4429205": "Bijan Robinson", "4429059": "Puka Nacua"}
POSITIONS = {"4429795": "RB", "4362628": "WR", "3916433": "RB",
             "4429205": "RB", "4429059": "WR"}


class Live:
    """The running watch and everything the fake socket saw leave it."""

    def __init__(self, draft_watch, sent: list[str]) -> None:
        self.watch = draft_watch
        self.sent = sent

    @property
    def queue(self):
        return self.watch.queue

    @property
    def queue_echoes(self):
        return self.watch.queue_echoes

    def echo(self, line: str) -> None:
        """ESPN sending a line to us, rather than us sending one to ESPN."""
        asyncio.run(self.watch.handle_line(line))


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A watch whose socket echoes back whatever it is sent, wired into _WATCHES."""
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    monkeypatch.setattr(watch, "STATE_DIR", tmp_path)
    monkeypatch.setattr(board, "espn_maps", lambda: (dict(NAMES), dict(POSITIONS)))
    league = LeagueSettings(name="t", teams=12, draft_slot=4, rounds=14)

    async def notify(content, meta):
        return None

    w = watch.DraftWatch("L", 2026, 3, "{A}", "s2", league, None, notify)
    w.connected = True
    w.espn_map = dict(NAMES)
    sent: list[str] = []

    class Ws:
        async def send(self, text):
            sent.append(text)
            await w.handle_line(text.strip())

    monkeypatch.setattr(w, "ws", Ws())

    b = pd.DataFrame({"name": list(NAMES.values()),
                      "position": [POSITIONS[k] for k in NAMES],
                      "espn_id": [int(k) for k in NAMES]})
    b["_key"] = b["name"].map(board.norm_name)
    monkeypatch.setattr(server, "_build_board", lambda force=False: b)
    monkeypatch.setattr(board, "resolve_espn_id",
                        lambda name, board_df, espn_map: (
                            next((int(k) for k, v in NAMES.items()
                                  if board.norm_name(v) == board.norm_name(name)), None),
                            f"no match for {name!r}"))
    monkeypatch.setitem(server._WATCHES, "L", (w, None))
    return Live(w, sent)


def _call(**kw) -> dict:
    return json.loads(asyncio.run(server.set_draft_queue(league_id="L", **kw)))


def _ids(rows: list[dict]) -> list[int]:
    return [r["espn_id"] for r in rows]


def _seed_user_queue(live: Live) -> None:
    """The user built a queue in the ESPN app and ESPN echoed it."""
    live.echo("DRAFT_LIST " + " ".join(str(i) for i in USER_QUEUE))
    assert live.queue == USER_QUEUE


class TestMergeIsTheDefault:
    def test_our_names_go_first_and_the_users_players_are_all_kept(self, live):
        _seed_user_queue(live)

        out = _call(player_names="Bijan Robinson, Puka Nacua")

        assert out["mode"] == "merge"
        assert _ids(out["sent"]) == OURS + USER_QUEUE
        assert _ids(out["accepted"]) == OURS + USER_QUEUE
        assert out["removed"] == []
        # The report says which entries were already the user's, so a reader can
        # see the tool did not invent them.
        assert _ids(out["kept_from_the_users_queue"]) == USER_QUEUE

    def test_the_player_who_went_missing_survives_a_merge(self, live):
        """The report that opened this: a player was in the queue before pick 61
        and gone afterwards. Under a merge he is still there."""
        _seed_user_queue(live)

        out = _call(player_names="Bijan Robinson")

        names = [r["name"] for r in out["accepted"]]
        assert "Cam Skattebo" in names
        assert live.queue is not None and 3916433 in live.queue

    def test_a_player_we_send_that_is_already_queued_is_not_duplicated(self, live):
        _seed_user_queue(live)

        out = _call(player_names="Ja'Marr Chase, Bijan Robinson")

        sent = _ids(out["sent"])
        assert sent == [4362628, 4429205, 4429795, 3916433]
        assert len(sent) == len(set(sent))


class TestReplaceIsExplicitAndNamesWhatItRemoved:
    def test_replace_sends_only_ours(self, live):
        _seed_user_queue(live)

        out = _call(player_names="Bijan Robinson, Puka Nacua", replace=True)

        assert out["mode"] == "replace"
        assert _ids(out["sent"]) == OURS
        assert _ids(out["accepted"]) == OURS

    def test_every_removed_player_is_named(self, live):
        _seed_user_queue(live)

        out = _call(player_names="Ja'Marr Chase", replace=True)

        # Chase was on both lists, so he is not a removal; the other two are.
        assert sorted(r["name"] for r in out["removed"]) == ["Cam Skattebo", "Jahmyr Gibbs"]
        assert _ids(out["queue_before"]) == USER_QUEUE


class TestTheReportDescribesWhatEspnDidNotWhatWeMeant:
    """`accepted` is ESPN's echo. `removed` is computed from it, not from the
    list we intended to send, because ESPN drops ids it rejects -- an
    already-drafted player is the ordinary case. Reporting the intent would say
    nothing was removed while one of the user's players had gone, which is the
    same shape as the defect this tool exists to end."""

    def _drop(self, live, dropped: int):
        """A socket that accepts everything except one id, as ESPN does."""
        class Picky:
            async def send(self, text):
                live.sent.append(text)
                kept = [f for f in text.split()[1:] if f != str(dropped)]
                await live.watch.handle_line("DRAFT_LIST " + " ".join(kept))

        live.watch.ws = Picky()

    def test_a_merge_that_espn_trims_reports_the_loss(self, live):
        _seed_user_queue(live)
        self._drop(live, 3916433)          # ESPN rejects him: already drafted

        out = _call(player_names="Bijan Robinson")

        assert out["mode"] == "merge"
        assert [r["name"] for r in out["removed"]] == ["Cam Skattebo"]
        assert 3916433 not in _ids(out["accepted"])

    def test_a_replace_reports_only_what_espn_actually_dropped(self, live):
        _seed_user_queue(live)
        self._drop(live, 4429205)          # ESPN rejects the one we are adding

        out = _call(player_names="Bijan Robinson", replace=True)

        # Everything the user had is gone, and the player we tried to add never
        # arrived; the report says both rather than claiming our list took effect.
        assert sorted(r["name"] for r in out["removed"]) == [
            "Cam Skattebo", "Ja'Marr Chase", "Jahmyr Gibbs"]
        assert out["accepted"] == []


class TestItWaitsForEspnsOwnEchoBeforeRefusing:
    """ESPN sends the first echo unprompted a few seconds after joining -- 3.7s
    on the 2026-09-05 join -- so a fresh connection is a brief window, not a
    state to refuse from. Waiting turns almost every refusal into a merge.

    The wait is on the watch's `queue_seen` event, so these tests drive the
    event rather than racing a timer.
    """

    def test_an_echo_that_lands_during_the_wait_is_merged_into(self, live):
        assert live.queue is None

        async def go():
            waiting = asyncio.ensure_future(server._await_first_echo(live.watch, 5.0))
            # Nothing has arrived, so the wait is still pending on the event.
            assert not waiting.done()
            await live.watch.handle_line(
                "DRAFT_LIST " + " ".join(str(i) for i in USER_QUEUE))
            return await waiting

        assert asyncio.run(go()) == USER_QUEUE

    def test_a_queue_already_echoed_returns_without_waiting(self, live):
        _seed_user_queue(live)
        assert asyncio.run(server._await_first_echo(live.watch, 5.0)) == USER_QUEUE

    def test_the_wait_ends_and_the_call_refuses_when_no_echo_comes(self, live, monkeypatch):
        # A deadline on real work, not a pause: the event never fires here, and
        # the constant is read at call time so a test can shorten it.
        monkeypatch.setattr(server, "QUEUE_ECHO_WAIT_SECONDS", 0.01)

        out = _call(player_names="Bijan Robinson")

        assert "replace=True" in out["error"]
        assert live.sent == []

    def test_the_event_is_cleared_by_a_reconnect(self, live):
        _seed_user_queue(live)
        assert live.watch.queue_seen.is_set()

        live.watch._reset_for_connection()

        assert not live.watch.queue_seen.is_set()

    def test_the_wait_does_not_disturb_set_queues_own_echo_future(self, live):
        """`queue_seen` is separate from `queue_echo`, which `set_queue` owns
        and replaces per call."""
        _seed_user_queue(live)

        out = _call(player_names="Bijan Robinson")

        assert out["mode"] == "merge"
        assert live.watch.queue_echo is None


class TestAQueuedPlayerWhoHasBeenDrafted:
    """ESPN sends no DRAFT_LIST when a pick empties a slot in your queue, so the
    last echo keeps naming players who are gone.

    Live at pick 135: the echo still had Jayden Reed at rank 3, taken thirteen
    picks earlier. Autopick skips him, so nothing breaks -- the payload simply
    stated a queue ESPN would not use.
    """

    FIXTURE = Path(__file__).parent / "fixtures" / "espn_draft_init.b64"
    # Ids the captured snapshot has NOT drafted. The module-level USER_QUEUE
    # cannot be used here: two of its three are already picks 1 and 4 in that
    # snapshot, so every row would come back marked drafted and the test would
    # assert nothing about the annotation.
    QUEUE = [3916433, 4429205, 4429059]

    def _joined(self, live):
        """The watch joined with the captured snapshot, so it has a pick log."""
        live.echo("INIT " + self.FIXTURE.read_text().strip())
        return len(espn_live.picks_from_init(
            espn_live.decode_init(self.FIXTURE.read_text().strip())))

    def test_a_merge_does_not_send_a_drafted_player_back(self, live):
        """2026-09-05, pick 141: a merge kept three drafted players from the
        stale echo and sent them back; ESPN accepted them. The log knew."""
        joined = self._joined(live)
        live.echo("DRAFT_LIST " + " ".join(str(i) for i in self.QUEUE))
        live.echo(f"SELECTED 7 {self.QUEUE[1]} 4 {{A}}")

        out = _call(player_names="Cam Skattebo")

        assert _ids(out["sent"]) == [self.QUEUE[0], self.QUEUE[2]]
        assert _ids(out["dropped_as_drafted"]) == [self.QUEUE[1]]
        assert out["dropped_as_drafted"][0]["drafted_at"] == joined + 1

    def test_a_drafted_player_asked_for_by_name_is_dropped_too(self, live):
        self._joined(live)
        live.echo("DRAFT_LIST " + " ".join(str(i) for i in self.QUEUE))
        live.echo(f"SELECTED 7 {self.QUEUE[1]} 4 {{A}}")

        out = _call(player_names="Bijan Robinson, Cam Skattebo")

        assert _ids(out["sent"]) == [self.QUEUE[0], self.QUEUE[2]]
        assert _ids(out["dropped_as_drafted"]) == [self.QUEUE[1], self.QUEUE[1]]

    def test_a_drafted_queue_entry_is_marked_and_left_out_of_effective(self, live):
        joined = self._joined(live)
        live.echo("DRAFT_LIST " + " ".join(str(i) for i in self.QUEUE))
        # ESPN takes the second man on the queue, and sends no new DRAFT_LIST.
        live.echo(f"SELECTED 7 {self.QUEUE[1]} 4 {{A}}")

        out = json.loads(asyncio.run(server.draft_queue(league_id="L")))

        assert _ids(out["as_echoed"]) == self.QUEUE, "what ESPN said, verbatim"
        marked = {r["espn_id"]: r["drafted_at"] for r in out["as_echoed"]}
        assert marked[self.QUEUE[1]] == joined + 1, "the pick that took him"
        assert marked[self.QUEUE[0]] is None and marked[self.QUEUE[2]] is None
        # What autopick would actually draw from, renumbered.
        assert _ids(out["effective"]) == [self.QUEUE[0], self.QUEUE[2]]
        assert [r["rank"] for r in out["effective"]] == [1, 2]
        assert out["drafted_since_the_echo"] == 1

    def test_an_untouched_queue_says_so_on_every_row(self, live):
        self._joined(live)
        live.echo("DRAFT_LIST " + " ".join(str(i) for i in self.QUEUE))

        out = json.loads(asyncio.run(server.draft_queue(league_id="L")))

        assert all(r["drafted_at"] is None for r in out["as_echoed"])
        assert _ids(out["effective"]) == self.QUEUE
        assert out["drafted_since_the_echo"] == 0

    def test_the_echo_history_carries_no_annotation_at_all(self, live):
        """It records what ESPN said at the time. Marking those rows with what
        happened afterwards would make a log of the past disagree with itself.

        The key is absent, not null. A null on a row nobody checked is a claim
        that the player is available, which is the claim this key exists to make
        honest.
        """
        self._joined(live)
        live.echo("DRAFT_LIST " + " ".join(str(i) for i in self.QUEUE))
        live.echo(f"SELECTED 7 {self.QUEUE[1]} 4 {{A}}")

        out = json.loads(asyncio.run(server.draft_queue(league_id="L")))

        assert all("drafted_at" not in r for r in out["echoes"][0]["queue"])
        # The row is otherwise whole, so this is a decision and not a dropped field.
        assert _ids(out["echoes"][0]["queue"]) == self.QUEUE

    def test_a_watch_with_no_snapshot_says_it_could_not_check(self, live):
        """The annotation needs the INIT payload. Without it, three different
        things used to arrive as `drafted_at: null` -- checked and still there,
        not checked by design, and could not be checked -- and the last reads as
        a clean bill of health.

        Nothing is claimed here instead. The queue is still returned, because a
        queue that cannot be annotated is still worth showing.
        """
        live.echo("DRAFT_LIST " + " ".join(str(i) for i in self.QUEUE))

        out = json.loads(asyncio.run(server.draft_queue(league_id="L")))

        assert _ids(out["as_echoed"]) == self.QUEUE
        assert all("drafted_at" not in r for r in out["as_echoed"])
        # Not the queue, which would repeat the false claim, and not [], which
        # would say the queue is empty.
        assert out["effective"] is None
        assert out["drafted_since_the_echo"] is None
        assert "no INIT" in out["pick_log"]

    def test_the_reason_is_reported_when_the_log_was_read_too(self, live):
        """A field that appears only on failure makes its own absence the signal,
        which is the thing stating `drafted_at` on every row exists to avoid."""
        self._joined(live)
        live.echo("DRAFT_LIST " + " ".join(str(i) for i in self.QUEUE))

        out = json.loads(asyncio.run(server.draft_queue(league_id="L")))

        assert out["pick_log"].startswith("read:")

    def test_the_send_says_which_of_its_rows_are_already_gone(self, live):
        """`removed` is the ids ESPN dropped, and an already-drafted player is the
        ordinary reason it drops one. The tool exists to report the outcome rather
        than the intent, so the reason belongs on the row."""
        joined = self._joined(live)
        live.echo("DRAFT_LIST " + " ".join(str(i) for i in self.QUEUE))
        live.echo(f"SELECTED 7 {self.QUEUE[1]} 4 {{A}}")

        async def go():
            w, _task = server._WATCHES["L"]
            return await server.merge_queue_ids(w, [self.QUEUE[0]], league_id="L")

        out = asyncio.run(go())

        assert out["pick_log"].startswith("read:")
        before = {r["espn_id"]: r["drafted_at"] for r in out["queue_before"]}
        assert before[self.QUEUE[1]] == joined + 1, (
            "the player ESPN will drop is marked with the pick that took him")
        assert before[self.QUEUE[0]] is None


class TestTheInitQueueIsObservedNotUsed:
    """`nomination_list` matched the first echo exactly on one join. One
    observation of a field named for auction nominations is not a contract, so
    the watch records whether it matches and nothing reads the answer."""

    def test_a_matching_init_queue_is_recorded_as_matched(self, live):
        live.watch.init_queue = list(USER_QUEUE)
        _seed_user_queue(live)

        assert live.watch.init_queue_checks == [
            {"connection": 0, "had_init_queue": True, "matched": True,
             "init_size": 3, "echo_size": 3}]

    def test_a_disagreement_is_recorded_rather_than_resolved(self, live):
        live.watch.init_queue = [4429795]
        _seed_user_queue(live)

        assert live.watch.init_queue_checks[0]["matched"] is False
        # The queue in use is ESPN's echo either way; observing changed nothing.
        assert live.queue == USER_QUEUE

    def test_order_counts_not_just_membership(self, live):
        live.watch.init_queue = list(reversed(USER_QUEUE))
        _seed_user_queue(live)

        assert live.watch.init_queue_checks[0]["matched"] is False

    def test_only_the_first_echo_of_a_connection_is_checked(self, live):
        live.watch.init_queue = list(USER_QUEUE)
        _seed_user_queue(live)
        live.echo("DRAFT_LIST 4429795")

        assert len(live.watch.init_queue_checks) == 1

    def test_an_init_without_a_queue_is_recorded_as_such(self, live):
        assert live.watch.init_queue is None
        _seed_user_queue(live)

        check = live.watch.init_queue_checks[0]
        assert check["had_init_queue"] is False and check["matched"] is None


class TestAnUnknownQueueIsNotOverwritten:
    def test_a_merge_is_refused_when_espn_has_echoed_nothing(self, live, monkeypatch):
        monkeypatch.setattr(server, "QUEUE_ECHO_WAIT_SECONDS", 0.01)
        assert live.queue is None

        out = _call(player_names="Bijan Robinson")

        assert live.sent == [], "nothing may go over the socket"
        assert _ids(out["would_send"]) == [4429205]
        # The one sentence a caller reads has to carry both the way out and what
        # it costs, or the refusal is just an obstacle.
        assert "replace=True" in out["error"]
        assert "overwrites" in out["error"]

    def test_replace_still_works_without_an_echo_because_it_was_asked_for(self, live):
        assert live.queue is None

        out = _call(player_names="Bijan Robinson", replace=True)

        assert out["mode"] == "replace"
        assert _ids(out["sent"]) == [4429205]
        # Nothing is claimed to have been removed, because nothing was known.
        assert out["removed"] == []


class TestAnInitQueueIsMergedIntoWhenNoEchoComes:
    """The resume of 2026-09-05: INIT carried the user's five entries, no
    DRAFT_LIST arrived inside the wait, and the queue was replaced with a
    message saying ESPN held none. INIT is ESPN saying what it holds."""

    def test_the_users_init_queue_is_kept_and_the_mode_says_where_it_came_from(
            self, live, monkeypatch):
        monkeypatch.setattr(server, "QUEUE_ECHO_WAIT_SECONDS", 0.01)
        live.watch.init_queue = list(USER_QUEUE)
        assert live.queue is None

        out = _call(player_names="Bijan Robinson")

        assert out["mode"] == "merge_from_init"
        assert _ids(out["sent"]) == [4429205] + USER_QUEUE
        assert _ids(out["kept_from_the_users_queue"]) == USER_QUEUE
        assert out["removed"] == []

    def test_an_echo_still_wins_over_init(self, live):
        live.watch.init_queue = [4429795]
        _seed_user_queue(live)

        out = _call(player_names="Bijan Robinson")

        assert out["mode"] == "merge"
        assert _ids(out["kept_from_the_users_queue"]) == USER_QUEUE


class TestAReconnectDoesNotCarryTheQueueForward:
    """`run()` reconnects, and the queue belongs to one socket session.

    Carrying it forward made the refusal mean "no echo ever on this watch
    object" while its message said "on this connection", so the guard could be
    skipped in the case it was written for. ESPN drops the queue when a session
    ends, so the stale list can describe a queue that is no longer there.
    """

    def test_the_reset_clears_the_queue_and_counts_the_connection(self, live):
        _seed_user_queue(live)
        assert live.watch.connection == 0

        live.watch._reset_for_connection()

        assert live.queue is None
        assert live.watch.connection == 1
        assert live.watch.ready.is_set() is False

    def test_the_refusal_fires_again_after_a_reconnect(self, live, monkeypatch):
        monkeypatch.setattr(server, "QUEUE_ECHO_WAIT_SECONDS", 0.01)
        _seed_user_queue(live)
        live.watch._reset_for_connection()

        out = _call(player_names="Bijan Robinson")

        assert "replace=True" in out["error"]
        assert live.sent == [], "a stale queue must not be merged into"

    def test_the_history_survives_the_reconnect_and_marks_it(self, live):
        _seed_user_queue(live)
        live.watch._reset_for_connection()
        live.echo("DRAFT_LIST 4429795")

        # The list shrank across a connection boundary, which the history shows
        # so nobody reads it as somebody editing the queue.
        assert [c for _ts, c, _ids in live.queue_echoes] == [0, 1]


class TestTheEchoHistory:
    def test_every_echo_is_kept_with_a_timestamp(self, live):
        _seed_user_queue(live)
        live.echo("DRAFT_LIST 4429795 4362628")

        assert [ids for _ts, _c, ids in live.queue_echoes] == [USER_QUEUE,
                                                               [4429795, 4362628]]
        assert all(ts > 0 for ts, _c, _ids in live.queue_echoes)

    def test_draft_queue_reports_the_history_so_a_loss_has_a_when(self, live):
        _seed_user_queue(live)
        live.echo("DRAFT_LIST 4429795 4362628")

        out = json.loads(asyncio.run(server.draft_queue(league_id="L")))

        assert len(out["echoes"]) == 2
        gone = set(_ids(out["echoes"][0]["queue"])) - set(_ids(out["echoes"][1]["queue"]))
        assert gone == {3916433}

    def test_each_echo_says_which_connection_it_came_from(self, live):
        """A list that shrank across a reconnect was not necessarily edited by
        anyone: ESPN drops the queue when a session ends."""
        _seed_user_queue(live)
        live.watch.connection = 2          # as a reconnect would leave it
        live.echo("DRAFT_LIST 4429795")

        assert [c for _ts, c, _ids in live.queue_echoes] == [0, 2]
        out = json.loads(asyncio.run(server.draft_queue(league_id="L")))
        assert [e["connection"] for e in out["echoes"]] == [0, 2]

    def test_an_id_espn_sends_that_cannot_be_read_does_not_kill_the_session(self, live):
        # `--5` passes a lstrip-then-isdigit test and then raises in int(). The
        # same split cost two fixes elsewhere in this package; the queue reader
        # lets int() decide.
        live.echo("DRAFT_LIST 4429795 --5 4362628")
        assert live.queue == [4429795, 4362628]
