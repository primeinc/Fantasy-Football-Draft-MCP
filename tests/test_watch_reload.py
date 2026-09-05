"""A watch built by older code must survive a reload of the code running it.

Observed live on 2026-09-05: after reloading main, `draft_queue` raised because
the running watch predated the commit that added `queue_echoes`. The tool error
was the mild half. The reader loop appends to `queue_echoes` on every DRAFT_LIST
and increments `connection` on reconnect, so ESPN's next echo would have raised
inside the socket loop, mid-draft.
"""
import ast
import asyncio
import inspect
import json
import sys

import pandas as pd
import pytest

from ffdraft import board, server, watch, watchstore
from ffdraft.config import LeagueSettings


def _watch(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    monkeypatch.setattr(watch, "STATE_DIR", tmp_path)
    monkeypatch.setattr(board, "espn_maps", lambda: ({"11": "Player Eleven"},
                                                     {"11": "RB"}))
    league = LeagueSettings(name="t", teams=12, draft_slot=4, rounds=14)

    async def notify(content, meta):
        return None

    w = watch.DraftWatch("L", 2026, 3, "{A}", "s2", league, None, notify)
    w.connected = True
    w.espn_map = {"11": "Player Eleven"}
    return w


class TestTheStateTablesCoverEveryField:
    """The migration is a hand-written table beside `__init__`, so the thing that
    keeps it honest is this test rather than anyone's diligence. A field added
    without a decision fails here instead of at the next live reload."""

    def _assigned_in_init(self) -> list[str]:
        src = inspect.getsource(watch.DraftWatch.__init__)
        tree = ast.parse(src.lstrip().replace("\n    ", "\n"))
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"):
                        names.append(target.attr)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Attribute):
                names.append(node.target.attr)
        return names

    def _tables(self) -> list[set]:
        return [set(watch.REBUILDABLE_STATE), set(watch.CONSTRUCTED_STATE),
                set(watch.REBOUND_CODE)]

    def test_every_attribute_init_sets_is_classified(self):
        assigned = set(self._assigned_in_init())
        classified = set().union(*self._tables())

        missing = sorted(assigned - classified)
        assert missing == [], (
            f"__init__ sets {missing} and no table names them. Decide: can a reload "
            "rebuild it from nothing (REBUILDABLE_STATE), did it come from the "
            "constructor (CONSTRUCTED_STATE), or is it code the reload must replace "
            "(REBOUND_CODE)?")

    def test_no_table_names_a_field_that_no_longer_exists(self):
        assigned = set(self._assigned_in_init())
        classified = set().union(*self._tables())

        stale = sorted(classified - assigned)
        assert stale == [], f"the tables still name {stale}, which __init__ no longer sets"

    def test_the_tables_do_not_overlap(self):
        tables = self._tables()
        for i, first in enumerate(tables):
            for second in tables[i + 1:]:
                both = first & second
                assert both == set(), f"{sorted(both)} is in two tables; it is one"

    def test_every_rebuildable_factory_makes_a_fresh_value(self):
        """Mutable state must not be shared between instances, which is exactly
        what class-level defaults would have done."""
        for name, factory in watch.REBUILDABLE_STATE.items():
            first, second = factory(), factory()
            if isinstance(first, (list, dict, set)):
                assert first is not second, f"{name} hands out one shared object"


class TestMigratingAnOldInstance:
    def test_a_missing_attribute_is_added(self, tmp_path, monkeypatch):
        w = _watch(tmp_path, monkeypatch)
        del w.queue_echoes                      # as a pre-reload instance has it

        result = watch.migrate_instance(w)

        assert "queue_echoes" in result["added"]
        assert w.queue_echoes == []

    def test_state_the_draft_built_is_never_overwritten(self, tmp_path, monkeypatch):
        """The reason the object is kept rather than rebuilt."""
        w = _watch(tmp_path, monkeypatch)
        w.picks_seen = 122
        w.queue = [11, 22]
        del w.queue_echoes

        watch.migrate_instance(w)

        assert w.picks_seen == 122 and w.queue == [11, 22]

    def test_a_constructor_field_that_is_missing_is_reported_not_invented(
            self, tmp_path, monkeypatch):
        w = _watch(tmp_path, monkeypatch)
        del w.swid

        result = watch.migrate_instance(
            w, watch.DraftWatch,
            code={"notify": server._channel, "refresh": server._watch_refresh})

        # It cannot be rebuilt from nothing, so the migration says so rather than
        # putting an empty string where a credential belongs.
        assert result["cannot_rebuild"] == ["swid"]
        assert not hasattr(w, "swid")

    def test_the_class_is_rebound_so_the_new_methods_run(self, tmp_path, monkeypatch):
        """Half the defect: after `importlib.reload` the instance still points at
        the OLD class object, so it keeps running the OLD methods while
        `reload_code` reports success."""
        w = _watch(tmp_path, monkeypatch)

        class Stale(watch.DraftWatch):
            pass

        w.__class__ = Stale
        result = watch.migrate_instance(w, watch.DraftWatch)

        assert result["class_rebound"] is True
        assert type(w) is watch.DraftWatch


class TestTheCallablesTheClassRebindDoesNotTouch:
    """`__class__` rebinding moves the METHODS. A function held in an attribute
    is untouched, so a watch that predates a reload keeps running the old body.

    Measured while building this: the old callables' `__globals__` is still the
    server module's dict, because server.py reloads in place, so the NAMES they
    look up resolve to the new functions. What is stale is the body itself.
    """

    def test_notify_and_refresh_are_rebound_to_the_current_module(
            self, tmp_path, monkeypatch):
        w = _watch(tmp_path, monkeypatch)

        async def old_notify(content, meta):
            return None

        w.notify = old_notify
        w.refresh = lambda: "OLD BODY"

        report = watch.migrate_instance(
            w, watch.DraftWatch,
            code={"notify": server._channel, "refresh": server._watch_refresh})

        assert sorted(report["rebound"]) == ["notify", "refresh"]
        assert w.notify is server._channel
        assert w.refresh is server._watch_refresh

    def test_a_callable_already_current_is_not_reported_as_rebound(
            self, tmp_path, monkeypatch):
        w = _watch(tmp_path, monkeypatch)
        w.notify = server._channel
        w.refresh = server._watch_refresh

        report = watch.migrate_instance(
            w, watch.DraftWatch,
            code={"notify": server._channel, "refresh": server._watch_refresh})

        assert report["rebound"] == []

    def test_a_replacement_nobody_supplied_is_named_not_skipped(
            self, tmp_path, monkeypatch):
        w = _watch(tmp_path, monkeypatch)

        report = watch.migrate_instance(w, watch.DraftWatch, code={})

        assert sorted(report["cannot_rebuild"]) == [
            "notify: no replacement supplied", "refresh: no replacement supplied"]

    def test_after_a_reload_the_watch_sees_the_reloaded_board(
            self, tmp_path, monkeypatch):
        """The end the task is about: a recommendation computed after a reload
        must go through the module that was just loaded."""
        w = _watch(tmp_path, monkeypatch)
        w.refresh = lambda: ("OLD BOARD", 0.0)
        monkeypatch.setitem(server._WATCHES, "L", (w, None))

        server.reload_package()
        live = sys.modules["ffdraft.server"]
        monkeypatch.setattr(live, "_build_board", lambda force=False: "NEW BOARD")
        monkeypatch.setattr(live, "_settings",
                            lambda: (None, type("W", (), {"bye": 9.0})()))

        board_now, bye_now = w.refresh()

        assert board_now == "NEW BOARD" and bye_now == 9.0
        server._WATCHES.pop("L", None)


class TestALiveWatchGetsAResumeRecord:
    """A watch started before the record existed runs perfectly and would not
    come back after a restart, and nothing said so: `update_queue` and
    `mark_stopped` both answer None when there is no record."""

    def test_a_watch_with_no_record_gets_one(self, tmp_path, monkeypatch):
        w = _watch(tmp_path, monkeypatch)
        w.queue = [11, 22]
        monkeypatch.setitem(server._WATCHES, "L", (w, None))
        assert watchstore.load("L") is None

        result = server.reload_package()

        assert result["watches"]["migrated"]["L"]["record"] == "written"
        record = watchstore.load("L")
        assert record is not None
        assert (record.league_id, record.team_id, record.season) == ("L", 3, 2026)
        assert record.queue == [11, 22] and record.resume is True
        # The thing the silence was hiding: the queue can now be recorded at all.
        assert watchstore.update_queue("L", [33]) is not None
        server._WATCHES.pop("L", None)

    def test_an_existing_record_is_left_alone(self, tmp_path, monkeypatch):
        watchstore.save(watchstore.WatchRecord(
            league_id="L", team_id=3, season=2026, queue=[99]))
        w = _watch(tmp_path, monkeypatch)
        monkeypatch.setitem(server._WATCHES, "L", (w, None))

        result = server.reload_package()

        assert result["watches"]["migrated"]["L"]["record"] == "present"
        record = watchstore.load("L")
        assert record is not None and record.queue == [99]
        server._WATCHES.pop("L", None)

    def test_a_stopped_record_is_not_resurrected(self, tmp_path, monkeypatch):
        """`stop_watch` clears the flag and keeps the file, which is what makes
        it win here: a stopped watch reads as `present`, not as absent."""
        watchstore.save(watchstore.WatchRecord(league_id="L", team_id=3, season=2026))
        watchstore.mark_stopped("L")
        w = _watch(tmp_path, monkeypatch)
        monkeypatch.setitem(server._WATCHES, "L", (w, None))

        result = server.reload_package()

        assert result["watches"]["migrated"]["L"]["record"] == "present"
        record = watchstore.load("L")
        assert record is not None and record.resume is False
        server._WATCHES.pop("L", None)


class TestTheMigrationRunsTheReloadedBody:
    """`reload_package` re-executes server.py in place, so which body migrates a
    watch is decided by whether the call sits before or after that reload.

    Measured live at 5ba1482: the first reload after landing the rebind reported
    `cannot_rebuild: [notify, refresh]` and no `record`, and a second reload with
    nothing changed on disk reported `rebound: [notify, refresh], record:
    present`. Every server-side migration change took effect one reload late.

    The discriminator here needs no file on disk. A body patched into the module
    dict is exactly what the reload overwrites, so it can only run if it is
    called first.
    """

    def _stale(self, marker: dict):
        def stale(errors):
            marker["ran"] = True
            return {"migrated": {}, "failed": {}}
        return stale

    def test_a_body_the_reload_replaces_does_not_migrate(
            self, tmp_path, monkeypatch):
        marker: dict = {}
        w = _watch(tmp_path, monkeypatch)
        monkeypatch.setitem(server._WATCHES, "L", (w, None))
        monkeypatch.setattr(server, "_migrate_watches", self._stale(marker))

        server.reload_package()

        assert marker == {}, (
            "the migration ran before the reload, so it was the outgoing body; "
            "a change to _migrate_watches lands one reload late")
        server._WATCHES.pop("L", None)

    def test_the_first_reload_reports_what_the_current_body_does(
            self, tmp_path, monkeypatch):
        """The live symptom, in the shape it was seen: a report missing the key
        the body on disk writes."""
        w = _watch(tmp_path, monkeypatch)
        w.queue = [11]
        monkeypatch.setitem(server._WATCHES, "L", (w, None))
        monkeypatch.setattr(server, "_migrate_watches", self._stale({}))
        assert watchstore.load("L") is None

        result = server.reload_package()

        assert result["watches"]["migrated"]["L"]["record"] == "written"
        server._WATCHES.pop("L", None)

    def test_the_watch_is_handed_the_reloaded_callables_not_the_outgoing_ones(
            self, tmp_path, monkeypatch):
        """`_channel` and `_watch_refresh` are looked up inside the migration, so
        migrating first hands the watch the bodies that are on their way out --
        the same defect the rebind was added to fix, one level up."""
        w = _watch(tmp_path, monkeypatch)
        monkeypatch.setitem(server._WATCHES, "L", (w, None))

        async def outgoing_channel(content, meta=None):
            return None

        def outgoing_refresh():
            return ("OLD BOARD", 0.0)

        monkeypatch.setattr(server, "_channel", outgoing_channel)
        monkeypatch.setattr(server, "_watch_refresh", outgoing_refresh)

        server.reload_package()

        live = sys.modules["ffdraft.server"]
        assert w.notify is not outgoing_channel
        assert w.refresh is not outgoing_refresh
        assert w.notify is live._channel and w.refresh is live._watch_refresh
        server._WATCHES.pop("L", None)


class TestTheLiveFailure:
    """The exact sequence from 2026-09-05, end to end."""

    def _pre_reload(self, tmp_path, monkeypatch):
        w = _watch(tmp_path, monkeypatch)
        for name in ("queue_echoes", "connection", "init_queue_checks"):
            delattr(w, name)
        return w

    def test_without_the_migration_the_reader_raises_on_the_next_echo(
            self, tmp_path, monkeypatch):
        w = self._pre_reload(tmp_path, monkeypatch)

        with pytest.raises(AttributeError):
            asyncio.run(w.handle_line("DRAFT_LIST 11"))

    def test_without_the_migration_a_reconnect_raises(self, tmp_path, monkeypatch):
        w = self._pre_reload(tmp_path, monkeypatch)

        with pytest.raises(AttributeError):
            w._reset_for_connection()

    def test_after_reload_package_the_echo_the_reconnect_and_the_tool_all_work(
            self, tmp_path, monkeypatch):
        w = self._pre_reload(tmp_path, monkeypatch)
        b = pd.DataFrame({"name": ["Player Eleven"], "position": ["RB"],
                          "espn_id": [11]})
        b["_key"] = b["name"].map(board.norm_name)
        monkeypatch.setattr(server, "_build_board", lambda force=False: b)
        monkeypatch.setitem(server._WATCHES, "L", (w, None))

        result = server.reload_package()

        assert result["errors"] == {}, result["errors"]
        migrated = result["watches"]["migrated"]["L"]
        assert set(migrated["added"]) >= {"queue_echoes", "connection",
                                          "init_queue_checks"}
        assert migrated["cannot_rebuild"] == []

        # The three things that failed live, in order.
        asyncio.run(w.handle_line("DRAFT_LIST 11"))
        w._reset_for_connection()
        out = json.loads(asyncio.run(server.draft_queue(league_id="L")))

        assert [e["connection"] for e in out["echoes"]] == [0]
        assert w.connection == 1
        server._WATCHES.pop("L", None)

    def test_a_watch_that_cannot_be_migrated_does_not_stop_the_reload(
            self, tmp_path, monkeypatch):
        """A half-reloaded server is worse than a reported failure.

        A registry entry the migration cannot touch is reported under
        `cannot_rebuild` rather than raising: the reload has to finish, and the
        thing it could not do has to be visible in what it returns.
        """
        monkeypatch.setitem(server._WATCHES, "bad", ("not a watch", None))

        result = server.reload_package()

        assert result["tools"] is not None, "the reload still finished"
        reported = result["watches"]["migrated"]["bad"]
        assert reported["class_rebound"] is False
        assert reported["cannot_rebuild"], "the failure is named, not swallowed"
        assert reported["added"] == [], "nothing was written onto an object it does not understand"
        server._WATCHES.pop("bad", None)
