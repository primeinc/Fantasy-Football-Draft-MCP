"""One cookie jar, one league URL. Every ESPN read goes through
`board.espn_cookies` and `board.espn_league_url`.

Six copies of the brace-wrapping rule and the league path existed before this
test; a fix to one (the SWID braces, the host) reached one. The websocket
draft room in `espn_live` sends the cookie as a header on its own handshake
and is the one deliberate exception.
"""
from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "ffdraft"


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
    # espn_live: the draft socket's own read of the security token.
    # espn_dump: leagueHistory, which is not a league document.
    hits = _count("lm-api-reads.fantasy.espn.com")
    assert set(hits) <= {"board.py", "espn_live.py", "espn_dump.py"}, hits
    for name in hits:
        text = (SRC / name).read_text(encoding="utf-8")
        assert 'READS_HOST = "https://lm-api-reads.fantasy.espn.com"' in text, name
