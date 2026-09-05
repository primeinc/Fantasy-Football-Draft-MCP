"""reload_code: the running server keeps its object and state; tools follow the code."""
import asyncio
import importlib
import sys

from ffdraft import server


def _names(srv) -> set[str]:
    return {t.name for t in srv._tool_manager.list_tools()}


def _old_stays() -> str:
    return "old"


def _new_stays() -> str:
    return "new"


def _goes() -> str:
    return "old"


def _arrives() -> str:
    return "new"


def test_reload_order_lists_every_package_module():
    # A module absent from RELOAD_ORDER is simply never re-imported, and
    # reload_code reports success anyway, so the miss is silent until someone
    # edits that module and wonders why the change did not take. roomstats
    # reached the integration branch missing from the list; roles is next.
    import pathlib

    pkg = pathlib.Path(server.__file__).parent
    on_disk = {p.stem for p in pkg.glob("*.py")} - {"__init__", "server"}
    listed = set(server.RELOAD_ORDER)
    assert on_disk - listed == set(), f"modules missing from RELOAD_ORDER: {sorted(on_disk - listed)}"
    assert listed - on_disk == set(), f"RELOAD_ORDER names modules that do not exist: {sorted(listed - on_disk)}"


def test_sync_tools_replaces_adds_and_removes():
    live = server._Server("live")
    fresh = server._Server("fresh")
    live.add_tool(_old_stays, name="stays")
    live.add_tool(_goes, name="goes")
    fresh.add_tool(_new_stays, name="stays")
    fresh.add_tool(_arrives, name="arrives")

    changes = server._sync_tools(live, fresh)
    assert changes == {"added": ["arrives"], "removed": ["goes"], "reloaded": ["stays"]}
    assert _names(live) == {"stays", "arrives"}
    stays = live._tool_manager.get_tool("stays")
    assert stays is not None and stays.fn() == "new"


def test_reload_keeps_state_and_server_object():
    import pandas as pd

    live = server.mcp
    server._WATCHES["probe-league"] = ("watch", "task")
    server._BOARDS["probe-key"] = pd.DataFrame({"name": ["probe"]})
    before = _names(live)

    result = server.reload_package()

    reloaded = sys.modules["ffdraft.server"]
    assert result["errors"] == {}
    assert reloaded.mcp is live
    assert reloaded._WATCHES.get("probe-league") == ("watch", "task")
    assert reloaded._BOARDS["probe-key"]["name"].tolist() == ["probe"]
    assert _names(live) == before
    assert result["tools"]["removed"] == [] and result["tools"]["added"] == []
    reloaded._WATCHES.pop("probe-league", None)
    reloaded._BOARDS.pop("probe-key", None)


def test_reload_code_tool_reports_and_notifies():
    class Session:
        def __init__(self):
            self.sent = 0

        async def send_tool_list_changed(self):
            self.sent += 1

    class Ctx:
        session = Session()

    out = asyncio.run(server.reload_code(Ctx()))
    assert '"notified": "notifications/tools/list_changed"' in out
    assert Ctx.session.sent == 1


def _as_main(monkeypatch):
    """A second copy of server.py in the state a `-m` launch leaves behind.

    `python -m ffdraft.server` runs the file as `__main__`: the module object is
    registered under that name only, `ffdraft.server` never enters sys.modules,
    and `__spec__.name` still says `ffdraft.server`. Reproduced here rather than
    described, because the bug lives entirely in the gap between those names.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("ffdraft.server", server.__file__)
    assert spec is not None and spec.loader is not None, "could not load a second server.py"
    copy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(copy)
    copy.__name__ = "__main__"
    monkeypatch.setitem(sys.modules, "__main__", copy)
    # The absence IS the bug; assert the setup rather than trusting it.
    monkeypatch.delitem(sys.modules, "ffdraft.server", raising=False)
    assert spec.name == "ffdraft.server"
    assert "ffdraft.server" not in sys.modules
    return copy


def test_reload_works_when_the_server_was_launched_as_main(monkeypatch):
    """The live failure: reload_code returned `ImportError: module
    ffdraft.server not in sys.modules` and tools null, while every package
    module reloaded, so the process ran new package code under the old
    server.py."""
    copy = _as_main(monkeypatch)

    result = copy.reload_package()

    assert result["errors"] == {}, result["errors"]
    assert result["tools"] is not None
    # The tools were synced onto the object the transport is serving, not onto
    # the fresh one the re-executed body built.
    assert copy.__dict__["mcp"] is copy.mcp
    assert _names(copy.mcp)


def test_reloading_as_main_does_not_re_enter_the_entry_point(monkeypatch):
    """`main()` calls `mcp.run()`, which would open a second stdio loop inside
    the tool call that asked for the reload.

    It does not happen, and the reason is worth pinning rather than assuming:
    `importlib.reload` sets `__name__` to `__spec__.name` before re-executing
    the body, so the trailing `if __name__ == "__main__"` is False on a reload
    even though it was True at launch. Measured under a real `python -m` launch,
    not inferred. This test is what would notice if that ever changed.
    """
    copy = _as_main(monkeypatch)

    def explode() -> None:
        raise AssertionError("main() ran during a reload; a second stdio loop "
                             "would have started inside the tool call")

    monkeypatch.setattr(copy, "main", explode)
    result = copy.reload_package()

    assert result["errors"] == {}, result["errors"]
    # Renamed by the reload, which is also why a second reload needs no special
    # case: the module is reachable under its spec name from here on.
    assert copy.__name__ == "ffdraft.server"


def test_reload_survives_a_module_that_fails_to_import(monkeypatch):
    # reload_package skips modules not yet imported. Run alone, this file never
    # imports ffdraft.choice, the fake failure never fires, and the assertion
    # on result["errors"] fails; import it first.
    importlib.import_module("ffdraft.choice")
    real_reload = importlib.reload

    def broken(module):
        if module.__name__ == "ffdraft.choice":
            raise SyntaxError("half-saved file")
        return real_reload(module)

    monkeypatch.setattr(importlib, "reload", broken)
    result = server.reload_package()
    assert "choice" in result["errors"] and "SyntaxError" in result["errors"]["choice"]
    assert result["tools"] is not None
    assert sys.modules["ffdraft.server"].mcp is server.mcp
