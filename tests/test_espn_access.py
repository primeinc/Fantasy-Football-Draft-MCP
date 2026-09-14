"""One cookie jar, one league URL, one league getter. Every ESPN read of the
league document goes through `board.espn_league_get`, which builds on
`board.espn_cookies` and `board.espn_league_url`.

Six copies of the brace-wrapping rule and the league path existed before this
test; a fix to one (the SWID braces, the host) reached one. The websocket
draft room in `espn_live` sends the cookie as a header on its own handshake,
and `espn_dump` sends the kona client's own headers on every view it captures;
those are the deliberate exceptions.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

from ffdraft import board

SRC = Path(__file__).resolve().parents[1] / "src" / "ffdraft"
LEAGUE_GETTERS = {("board.py", "espn_league_get"), ("espn_dump.py", "_get")}


def _league_gets() -> set[tuple[str, str]]:
    """(file, enclosing function) of every `requests.get` call in a function that
    also names the league document: a call to `espn_league_url`, the name
    `READS_HOST`, or a string piece holding "/leagues/"; or the dump's `_get`,
    whose callers pass it the URL. It sees `requests.get` by that spelling only:
    `requests.request("GET", ...)`, a Session's `.get` or `from requests import
    get` would pass unseen, and none is used in src."""
    out = set()
    for p in sorted(SRC.glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            nodes = list(ast.walk(fn))
            calls = [n for n in nodes if isinstance(n, ast.Call)]
            gets = [c for c in calls if isinstance(c.func, ast.Attribute) and c.func.attr == "get"
                    and isinstance(c.func.value, ast.Name) and c.func.value.id == "requests"]
            league = (any(isinstance(c.func, ast.Name) and c.func.id == "espn_league_url"
                          for c in calls)
                      or any(isinstance(n, ast.Name) and n.id == "READS_HOST" for n in nodes)
                      or any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                             and "/leagues/" in n.value for n in nodes)
                      or (p.name == "espn_dump.py" and fn.name == "_get"))
            if gets and league:
                out.add((p.name, fn.name))
    return out


def test_every_league_read_goes_through_the_one_getter():
    assert _league_gets() == LEAGUE_GETTERS


def test_the_scan_finds_a_league_read_outside_the_getter(tmp_path, monkeypatch):
    # The control: the same scan over a file with a direct read reports it.
    (tmp_path / "rogue.py").write_text(
        "import requests\nfrom .board import espn_league_url\n"
        "def rogue(league_id, season):\n"
        "    return requests.get(espn_league_url(league_id, season))\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "SRC", tmp_path)
    assert _league_gets() == {("rogue.py", "rogue")}


def test_the_getter_sends_cookies_agent_and_a_filter_only_when_given(monkeypatch):
    sent = []
    monkeypatch.setattr(board.requests, "get", lambda url, **kw: sent.append((url, kw)))
    board.espn_league_get("123", 2026, {"view": "mTeam"}, "ABC", "s2")
    board.espn_league_get("123", 2026, {"view": "kona_player_info"}, "ABC", "s2",
                          player_filter={"players": {"limit": 1}}, timeout=20)
    (url, plain), (_, kona) = sent
    assert url == board.espn_league_url("123", 2026)
    assert plain == {"params": {"view": "mTeam"}, "cookies": {"SWID": "{ABC}", "espn_s2": "s2"},
                     "timeout": 30, "headers": {"User-Agent": "ffdraft-mcp/1.0"}}
    assert kona["timeout"] == 20 and kona["headers"] == {
        "User-Agent": "ffdraft-mcp/1.0", "X-Fantasy-Source": "kona",
        "X-Fantasy-Filter": json.dumps({"players": {"limit": 1}})}


def test_a_read_that_splits_the_league_path_across_strings_is_found(tmp_path, monkeypatch):
    # The control for the widened scan: espn_live's draftSecurity read built its URL
    # from READS_HOST and a "/leagues/" piece, calling no espn_league_url (angel).
    (tmp_path / "split.py").write_text(
        "import requests\nREADS_HOST = 'x'\n"
        "def token(league_id, season):\n"
        "    url = f'{READS_HOST}/seasons/{season}/segments/0' f'/leagues/{league_id}/teams'\n"
        "    return requests.get(url)\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "SRC", tmp_path)
    assert _league_gets() == {("split.py", "token")}


def _count(needle: str) -> dict[str, int]:
    out = {}
    for p in sorted(SRC.glob("*.py")):
        n = p.read_text(encoding="utf-8").count(needle)
        if n:
            out[p.name] = n
    return out


def test_the_cookie_jar_is_built_in_exactly_one_place():
    assert _count('"SWID": swid if swid.startswith("{")') == {"board.py": 1}


def test_the_league_path_is_built_in_exactly_one_place():
    assert _count("/segments/0/leagues/") == {"board.py": 1}


def test_the_read_host_literal_lives_only_in_named_constants():
    # espn_dump: leagueHistory, which is not a league document.
    hits = _count("lm-api-reads.fantasy.espn.com")
    assert set(hits) <= {"board.py", "espn_dump.py"}, hits
    for name in hits:
        text = (SRC / name).read_text(encoding="utf-8")
        assert 'READS_HOST = "https://lm-api-reads.fantasy.espn.com"' in text, name
