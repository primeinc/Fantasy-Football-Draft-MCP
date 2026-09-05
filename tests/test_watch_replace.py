"""Replacing a running watch cancels its task; the old socket does not outlive
the registry entry that named it.

`watch_draft` says "calling again replaces it". A replace that only overwrote
`_WATCHES[league_id]` left the previous task running its reader loop against
ESPN with nothing able to stop it (#53).
"""
from __future__ import annotations

import ast
import asyncio
import inspect

from ffdraft import server


def test_stop_watch_cancels_the_registered_task():
    class W:
        picks_seen = 3
        snapshots: list = []
        snapshot_failures = 0
        last_line = "CLOCK 1 2 3"

    async def run():
        async def forever():
            await asyncio.sleep(3600)
        task = asyncio.create_task(forever())
        server._WATCHES["probe-replace"] = (W(), task)
        await asyncio.sleep(0)
        out = await server.stop_watch("probe-replace")
        # Cancellation is observable only once the loop returns to the task.
        try:
            await task
        except asyncio.CancelledError:
            pass
        return out, task.cancelled(), "probe-replace" in server._WATCHES

    out, cancelled, still_registered = asyncio.run(run())
    assert '"stopped": true' in out
    assert cancelled
    assert not still_registered


def test_watch_draft_stops_the_previous_watch_before_registering_the_new_one():
    src = inspect.getsource(server.watch_draft)
    tree = ast.parse(src.lstrip().replace("\n    ", "\n") if src.startswith(" ") else src)
    order: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            fn = node.value.func
            if isinstance(fn, ast.Name) and fn.id == "stop_watch":
                order.append(("stop_watch", node.lineno))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) \
                        and t.value.id == "_WATCHES":
                    order.append(("register", node.lineno))
    kinds = [k for k, _ in sorted(order, key=lambda kv: kv[1])]
    assert kinds == ["stop_watch", "register"], kinds
