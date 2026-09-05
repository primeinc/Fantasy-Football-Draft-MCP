"""Every tool's payload leaves through `_emit`, and survives a board with holes.

`json.dumps` writes a float NaN as a bare `NaN` literal. Python's own parser
reads that back, so the failure is invisible from inside the process and total
from outside it: every conforming client rejects the response.

`_jsonable` already existed and guarded exactly one of the 83 `json.dumps` calls
in `server.py` — the one payload someone had already been burned by. The other
82 were one new column away from the same bug, and two of them were already
there: with sanitising disabled, `predict_pick` and `draft_replay` both emit a
bare constant on the board built below. `default=str` does not help, because
`json.dumps` writes the float itself and never consults `default`.

Three tests, and the third is the point of the other two. A round-trip test that
cannot fail proves nothing, so `test_the_round_trip_would_catch_a_regression`
turns the sanitising off and asserts that at least one tool does emit a bare
constant. If that test ever passes with nothing found, the board below has
stopped carrying holes and the test above it has gone vacuous.
"""
import ast
import json
import pathlib

import numpy as np
import pandas as pd
import pytest

from ffdraft import board as bd
from ffdraft import server

SERVER_PY = pathlib.Path(server.__file__)

# Tools that run on a board alone, no network and no draft state. Each one
# hand-builds at least part of its payload from row values.
TOOLS: list[tuple[str, dict]] = [
    ("best_available", {}),
    ("draft_status", {}),
    ("draft_audit", {}),
    ("who_should_i_pick", {}),
    ("value_picks", {}),
    ("plan_my_draft", {}),
    ("draft_strength", {}),
    ("rookie_report", {}),
    ("defense_report", {}),
    ("separation_report", {}),
    ("player_report", {"player_name": "Ja Chase"}),
    ("predict_pick", {}),
    ("draft_replay", {}),
]


def _reject(constant: str) -> None:
    """json.loads calls this for NaN / Infinity / -Infinity, and for nothing else."""
    raise AssertionError(f"payload carried a bare {constant} literal, which is not JSON")


@pytest.fixture
def poisoned_board(monkeypatch):
    """A board with a hole in every column a payload is built from.

    Holes, not absences: the column exists and one row's value is missing, which
    is what ESPN actually returns — no injury status for a team defense, no team
    for a player it does not carry, no ADP for someone nobody drafts.
    """
    league, _ = server._settings()
    b = pd.DataFrame({
        "name": ["Ja Chase", "Bijan Robinson", "Team DST", "Kicker Guy", "Rook Guy"],
        "position": ["WR", "RB", "DST", "K", "RB"],
        "team": ["CIN", np.nan, "HOU", "DAL", "NO"],
        "proj_points": [300.0, 250.0, 90.0, 130.0, 120.0],
        "draft_score": [300.0, 250.0, 90.0, 130.0, 120.0],
        "adp": [1.5, 2.2, 180.0, np.nan, 150.0],
        "pos_rank": [1, 1, 1, 1, 2],
        "overall_rank": [1, 2, 180, 200, 150],
        "consistency": [0.5, np.nan, 0.4, 0.6, 0.5],
        "adj_ppg": [15.0, 14.0, np.nan, 8.0, 7.0],
        "bye_week": [10, np.nan, 7, 9, 5],
        "espn_proj": [np.nan, 240.0, 88.0, 120.0, 110.0],
        "espn_injury": [np.nan, "ACTIVE", np.nan, "ACTIVE", "OUT"],
        "espn_rank": [1, 2, np.nan, 200, 150],
        "adp_source": ["espn", "espn", "espn", "undrafted", "espn"],
        "is_rookie": [False, False, False, False, True],
        "off_roster": [False, False, False, False, False],
    })
    b["_key"] = b["name"].map(bd.norm_name)
    key = league.cache_key()
    monkeypatch.setitem(server._BOARDS, key, b)
    return b


def test_every_payload_leaves_through_emit():
    """No handler may call `json.dumps` directly.

    This is the durable half. The round-trip below can only cover the tools that
    run without a network; a new handler that builds a payload from a live ESPN
    response would not be exercised by it, and would go straight back to the
    original bug. Here there is nothing to remember: the only `json.dumps` in
    the module is the one inside `_emit`.
    """
    tree = ast.parse(SERVER_PY.read_text(encoding="utf-8"), filename=str(SERVER_PY))
    inside_emit: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_emit":
            inside_emit = set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    assert inside_emit, "server._emit is gone; this test no longer checks anything"

    strays = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "dumps"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "json"
        and node.lineno not in inside_emit
    ]
    assert not strays, (
        "server.py calls json.dumps outside _emit, so these payloads are not "
        f"sanitised and can emit a bare NaN: lines {strays}"
    )


def test_tools_round_trip_a_nan_bearing_board(poisoned_board):
    """Every tool above returns JSON a conforming parser accepts."""
    failures = []
    for name, kwargs in TOOLS:
        raw = getattr(server, name)(**kwargs)
        try:
            json.loads(raw, parse_constant=_reject)
        except AssertionError as exc:
            failures.append(f"{name}: {exc}")
    assert not failures, "\n  ".join(["tools emitted non-JSON:"] + failures)


def test_the_round_trip_would_catch_a_regression(poisoned_board, monkeypatch):
    """The control. Turn the sanitising off; at least one tool must break.

    Without this, a board that stopped carrying holes would leave the test above
    green and empty, which is the same silent pass the whole file exists to
    prevent. Measured when written: `predict_pick` and `draft_replay` are the
    two that break, because they build rows from `espn_rank` and per-pick model
    output rather than through `_rows`.
    """
    monkeypatch.setattr(server, "_emit", lambda payload, **kw: json.dumps(payload, **kw))
    broke = []
    for name, kwargs in TOOLS:
        try:
            json.loads(getattr(server, name)(**kwargs), parse_constant=_reject)
        except AssertionError:
            broke.append(name)
    assert broke, (
        "with sanitising disabled every tool still returned clean JSON, so the "
        "round-trip test above cannot fail and is proving nothing; give the "
        "poisoned_board fixture a hole that reaches a payload"
    )
