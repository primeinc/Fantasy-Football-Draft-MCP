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

import pandas as pd
import pytest

from ffdraft import board, server, watch
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

    def test_every_attribute_init_sets_is_classified(self):
        assigned = set(self._assigned_in_init())
        classified = set(watch.REBUILDABLE_STATE) | set(watch.CONSTRUCTED_STATE)

        missing = sorted(assigned - classified)
        assert missing == [], (
            f"__init__ sets {missing} and neither table names them. Decide: can a "
            "reload rebuild it from nothing (REBUILDABLE_STATE) or did it come from "
            "the constructor (CONSTRUCTED_STATE)?")

    def test_neither_table_names_a_field_that_no_longer_exists(self):
        assigned = set(self._assigned_in_init())
        classified = set(watch.REBUILDABLE_STATE) | set(watch.CONSTRUCTED_STATE)

        stale = sorted(classified - assigned)
        assert stale == [], f"the tables still name {stale}, which __init__ no longer sets"

    def test_the_two_tables_do_not_overlap(self):
        both = set(watch.REBUILDABLE_STATE) & set(watch.CONSTRUCTED_STATE)
        assert both == set(), f"{sorted(both)} is in both tables; it is one or the other"

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

        result = watch.migrate_instance(w)

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
