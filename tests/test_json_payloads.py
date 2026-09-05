"""Every tool's payload leaves through `_emit`, and survives a board with holes.

`json.dumps` writes a float NaN as a bare `NaN` literal. Python's own parser
reads that back, so the failure is invisible from inside the process and total
from outside it: every conforming client rejects the response.

`_jsonable` already existed and guarded exactly one of the 83 `json.dumps` calls
in `server.py` — the one payload someone had already been burned by. The other
82 were one new column away from the same bug, and three of them were already
there: with sanitising disabled, `predict_pick`, `draft_replay` and
`plan_my_draft` all emit a bare constant on the board built below. `default=str`
does not help, because `json.dumps` writes the float itself and never consults
`default`.

The control is the point of the round trip. A round-trip test that cannot fail
proves nothing, so `test_the_round_trip_would_catch_a_regression` turns the
sanitising off and asserts which tools emit a bare constant — by name, so the
claim above is checked rather than remembered. If it ever finds nothing, the
board below has stopped carrying holes and the test above it has gone vacuous.

The file also owns the draft it replays (#56). It used to read whichever draft
was on the developer's machine, which cost 27 of its 28.5 seconds and made its
coverage depend on data nobody declared — `plan_my_draft` was the third tool
that breaks all along, and no run had ever gone down that path.
"""
import ast
import json
import pathlib
import time

import numpy as np
import pandas as pd
import pytest

from ffdraft import board as bd
from ffdraft import model, server

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


# Picks in the fixture draft. Two board players so the walk-forward predictors
# have something to fit and then score, the rest off-board, which is what a
# five-row board makes of any real draft anyway.
#
# This file is about payload SHAPE under holes, and draft length is incidental
# to that. It used to read the developer's own recorded draft -- 134 picks -- and
# `model.recommend` ran once per pick per replay, twice per tool, in three tests:
# 27 of the file's 28.5 seconds to prove `json.loads` accepts the output. The
# shapes do not depend on the length: at 134 picks the walk-forward scored
# exactly ONE of them against this board, and at 26 it scores two (#56).
FIXTURE_PICKS = 26


@pytest.fixture
def poisoned_board(monkeypatch, tmp_path):
    """A board with a hole in every column a payload is built from, and a short
    draft to replay it against.

    Holes, not absences: the column exists and one row's value is missing, which
    is what ESPN actually returns — no injury status for a team defense, no team
    for a player it does not carry, no ADP for someone nobody drafts.

    The draft is built here rather than read from `~/.ffdraft`. That is what
    makes the file cheap, and it also removes a dependency nobody declared: these
    tests were replaying whichever draft happened to be on the machine, so their
    cost and their coverage both moved with it.
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

    monkeypatch.setattr(bd, "STATE_DIR", tmp_path)
    state = bd.DraftState(league)
    on_the_board = {0: "Ja Chase", 1: "Bijan Robinson"}
    for i in range(FIXTURE_PICKS):
        state.record(on_the_board.get(i, f"Other Guy {i}"), i + 1,
                     (i % league.teams) + 1,
                     position=None if i in on_the_board else "RB")
    state.save()
    # Three board rows are left undrafted on purpose. With none available
    # `choice.forecast` reduces over an empty array and raises `ValueError:
    # zero-size array to reduction operation maximum`, which is a real hole in
    # `choice.probabilities` but not this file's subject.
    return b


def test_every_payload_leaves_through_emit():
    """No handler may call `json.dumps` directly.

    This is the durable half. The round-trip below can only cover the tools that
    run without a network; a new handler that builds a payload from a live ESPN
    response would not be exercised by it, and would go straight back to the
    original bug. Here there is nothing to remember: the only `json.dumps` in
    the module is the one inside `_emit`.
    """
    # The exit itself, named one by one rather than by a prefix, so a new
    # helper is not exempted by being called `_something`. `_under_the_cap` and
    # `_longest_list` measure and trim an already-sanitised payload on its way
    # out (#52); they are the exit, not handlers using it, and they cannot go
    # through `_emit` without calling it from inside itself.
    THE_EXIT = ("_emit", "_under_the_cap", "_longest_list")
    tree = ast.parse(SERVER_PY.read_text(encoding="utf-8"), filename=str(SERVER_PY))
    inside_emit: set[int] = set()
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in THE_EXIT:
            found.add(node.name)
            inside_emit |= set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    assert found == set(THE_EXIT), (
        f"server's JSON exit has changed shape: expected {THE_EXIT}, found "
        f"{sorted(found)}. Update this list deliberately -- it is what decides "
        f"which `json.dumps` calls are the exit and which are unsanitised handlers"
    )

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
    prevent.

    Three tools break: `predict_pick`, `draft_replay` and `plan_my_draft`. The
    first two build rows from `espn_rank` and per-pick model output rather than
    through `_rows`. `plan_my_draft` was found by the fixture draft this file
    now builds for itself (#56) -- the developer's own recorded draft never took
    it down that path, so the control had been passing on two tools while a
    third was equally unsanitised and nobody knew. Which is the argument for a
    fixture the test owns over one it happens to find.
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
    # The docstring above names which two, so it is checked rather than
    # remembered. A comment saying "measured when written" rots silently; this
    # fails and prints the new list, which is what a reader needs anyway.
    assert set(broke) == {"predict_pick", "draft_replay", "plan_my_draft"}, (
        f"the tools that break with sanitising off are now {sorted(broke)}, not "
        f"the two this test's docstring names. Update the docstring to match, "
        f"and check the change was deliberate")


def test_the_two_expensive_tools_stay_cheap(poisoned_board, monkeypatch):
    """The bound, so the cost cannot creep back (#56).

    `draft_replay` and `predict_pick` are the only tools here that replay the
    whole draft, and both must stay in `TOOLS` because the control below names
    them among the tools that break. So the cost is bounded rather than avoided.

    THE BOUND IS A CALL COUNT, NOT A STOPWATCH, and that is a change from how
    this was first written. The cost is `model.recommend` once per pick per
    replay, so the thing that can creep back is replaying more picks than the
    fixture records -- which is countable exactly. Wall clock measures that plus
    the machine: timing the same two calls ten times on this box gave 2.72 s to
    7.41 s, a 2.7x spread with nothing changed, so any threshold loose enough
    not to flake was too loose to catch a doubling. A count cannot flake and
    names the actual failure.

    Measured: **82** calls here. On the 134-pick draft this file used to read off
    the developer's disk, `draft_replay` alone made 270 and `predict_pick`
    another 135. In seconds that was 5.46 and 2.68, against 1.04 and 0.52 now.
    The ceiling is `4 * FIXTURE_PICKS`, 27% above the real number: it can only
    move if a tool starts replaying the draft an extra time, which is exactly
    the regression, and restoring the full draft overshoots it fivefold.

    The wall-clock check stays as a smoke test at a deliberately silly threshold.
    It is there to catch something pathological, not to measure anything.
    """
    calls = {"n": 0}
    real = model.recommend

    def counted(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(model, "recommend", counted)
    start = time.perf_counter()
    server.draft_replay()
    server.predict_pick()
    spent = time.perf_counter() - start

    # Two replays inside `draft_replay`, one inside `predict_pick`, each one
    # recommendation per recorded pick, plus the pick on the clock.
    ceiling = 4 * FIXTURE_PICKS
    assert calls["n"] <= ceiling, (
        f"the replay tools called model.recommend {calls['n']} times for a "
        f"{FIXTURE_PICKS}-pick fixture draft, over the {ceiling} this bounds. "
        f"Something is replaying more of the draft than the fixture records")
    assert spent < 30.0, f"the two replay tools took {spent:.1f}s, which is absurd"


class TestThePayloadCap:
    """A result over the client's limit is not shown truncated, it is dropped.

    `stream_kdst(week=1)` came back at 69,512 characters and the user could not
    read the tool at all (#52). Each tool shapes its own answer to a size a
    person can read; this is the backstop that makes the bound a guarantee
    rather than a habit, enforced at the one exit every payload leaves through.
    """

    def test_every_tool_fits_the_cap_on_a_real_board(self, poisoned_board):
        over = []
        for name, kwargs in TOOLS:
            size = len(getattr(server, name)(**kwargs))
            if size > server.PAYLOAD_LIMIT:
                over.append(f"{name}: {size:,}")
        assert not over, "\n  ".join(["tools over the client's limit:"] + over)

    def test_a_payload_over_the_cap_is_cut_down_and_says_so(self):
        rows = [{"name": f"Player {i}", "note": "x" * 200} for i in range(400)]
        text = server._emit({"season": 2026, "ranked": rows}, indent=2)
        assert len(text) <= server.PAYLOAD_LIMIT
        out = json.loads(text)
        # The head of the table, not a slice from the middle or a dropped key.
        assert out["ranked"][0]["name"] == "Player 0"
        assert len(out["ranked"]) < 400
        assert out["season"] == 2026
        # And it states the cut rather than presenting a short table as a whole
        # one, which is the difference between a trimmed answer and a wrong one.
        assert out["truncated"]["paths"]["ranked"] == f"{len(out['ranked'])} of 400"

    def test_the_longest_table_is_the_one_cut(self):
        # Two tables, one much larger. The small one is an answer; the large one
        # is what made the payload unreadable.
        small = [{"name": f"Kicker {i}"} for i in range(5)]
        large = [{"name": f"Defence {i}", "note": "y" * 200} for i in range(400)]
        out = json.loads(server._emit({"kickers": small, "defences": large}, indent=2))
        assert len(out["kickers"]) == 5
        assert len(out["defences"]) < 400
        assert "defences" in out["truncated"]["paths"]
        assert "kickers" not in out["truncated"]["paths"]

    def test_a_nested_table_is_reached_by_its_path(self):
        payload = {"weeks": [{"week": 1,
                              "ranked": [{"name": f"D {i}", "note": "z" * 200}
                                         for i in range(400)]}]}
        out = json.loads(server._emit(payload, indent=2))
        assert len(out["weeks"][0]["ranked"]) < 400
        assert any("ranked" in path for path in out["truncated"]["paths"])

    def test_what_cannot_be_cut_is_reported_rather_than_mangled(self):
        # One enormous string is not a table, so there is nothing to trim. The
        # answer must be a refusal a client can parse, not a truncated document
        # that is no longer JSON.
        out = json.loads(server._emit({"note": "q" * (server.PAYLOAD_LIMIT + 500)}))
        assert "does not fit" in out["error"]
        assert out["limit"] == server.PAYLOAD_LIMIT

    def test_the_note_explaining_the_cut_is_inside_the_cap(self):
        """The blocker lena found, on the payload shape that provokes it.

        The note used to be added after the size check and returned without
        re-checking, so the sentence explaining the trim pushed the answer back
        over the limit and the cap failed in the case it exists for. Many small
        tables is the shape that lands in that window: each pass halves exactly
        one list, so the descent is gradual and the first size under the cap
        tends to be just under it. Forty tables of thirty rows measured 21,064
        against a 20,000 cap before the fix.

        The single-list payload above cannot show this -- it halves to far below
        the limit and never approaches the boundary -- which is why this fixture
        exists beside it rather than instead of it.
        """
        payload = {f"table_{i:02d}": [{"name": f"row {j}", "note": "n" * 30}
                                      for j in range(30)]
                   for i in range(40)}
        assert len(json.dumps(payload, indent=2)) > server.PAYLOAD_LIMIT
        text = server._emit(payload, indent=2)
        assert len(text) <= server.PAYLOAD_LIMIT, (
            f"emitted {len(text):,} characters against a "
            f"{server.PAYLOAD_LIMIT:,} cap; the note that says the answer was "
            f"cut is not inside the size it reports on")
        body = json.loads(text)
        assert body["truncated"]["paths"]
        # And it landed close enough to the line that the note's own length
        # mattered, which is what makes this fixture the right one.
        assert len(text) > server.PAYLOAD_LIMIT - 1_000

    def test_a_bare_array_is_wrapped_rather_than_raising(self):
        # `holder is None` at the root, so the old code raised TypeError from
        # inside the one exit every payload goes through. No handler emits a
        # top-level list today; this is latent in `_emit`, which is the worst
        # place for it to be latent.
        rows = [{"name": f"row {j}", "note": "x" * 300} for j in range(200)]
        body = json.loads(server._emit(rows, indent=2))
        assert len(json.dumps(body)) <= server.PAYLOAD_LIMIT
        assert body["items"][0]["name"] == "row 0"
        assert len(body["items"]) < 200
        assert body["truncated"]["paths"]

    def test_a_payload_with_its_own_truncated_key_keeps_it(self):
        # Overwriting a tool's own field in order to report on the tool would be
        # its own small lie.
        payload = {"truncated": "mine",
                   "ranked": [{"name": f"P {i}", "note": "y" * 200}
                              for i in range(400)]}
        body = json.loads(server._emit(payload, indent=2))
        assert body["truncated_before_the_cap"] == "mine"
        assert body["truncated"]["paths"]["ranked"]

    def test_a_payload_already_under_the_cap_is_untouched(self):
        # The guarantee costs nothing on the answers that never needed it, and
        # `truncated` must not appear on an answer that was not truncated.
        payload = {"season": 2026, "ranked": [{"name": "A"}, {"name": "B"}]}
        out = json.loads(server._emit(payload, indent=2))
        assert out == payload
