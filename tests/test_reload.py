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


def test_reload_survives_a_module_that_fails_to_import(monkeypatch):
    # reload_package skips modules not yet imported, so make sure the one the
    # fake failure targets is loaded; run alone, this file would otherwise
    # never import ffdraft.choice and the test would pass for the wrong reason.
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
