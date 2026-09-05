import json
from pathlib import Path

import pytest

from ffdraft import espn_dump, espn_live

FIXTURE = Path(__file__).parent / "fixtures" / "espn_draft_init.b64"


class Resp:
    def __init__(self, url, status=200, body=None):
        self.url = url
        self.status_code = status
        self._body = body

    @property
    def content(self):
        return json.dumps(self._body).encode() if self._body is not None else b"nope"

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


@pytest.fixture
def api(monkeypatch):
    calls = []

    def fake_get(url, params, cookies, headers, timeout):
        calls.append((url, params, headers, cookies, timeout))
        view = params.get("view")
        if view == "mStatus":
            return Resp(url, status=404, body=None)
        return Resp(url, body={"view": view, "id": 1734659820})

    monkeypatch.setattr(espn_dump.requests, "get", fake_get)
    return calls


class TestDumpDraft:
    def test_writes_every_view_and_the_watch_snapshot(self, api, tmp_path):
        b64 = FIXTURE.read_text().strip()
        lines = [(1000, f"INIT {b64[:8]}"), (2000, "SELECTED 3 4362628 4")]
        m = espn_dump.dump_draft("1734659820", tmp_path, 2026, swid="{A}", espn_s2="s",
                                 init_b64=b64, lines=lines)

        root = Path(m["root"])
        assert root.parent == tmp_path.resolve()
        views = {e["view"]: e for e in m["read_api"]}
        assert set(espn_dump.READ_VIEWS) <= set(views)
        assert views["kona_player_info"]["status"] == 200
        assert views["leagueHistory"]["status"] == 200
        assert views["mStatus"]["status"] == 404
        assert m["errors"] == ["mStatus: HTTP 404"]
        assert (root / "read_api" / "mSettings.json").exists()
        assert (root / "read_api" / "mStatus.json").read_bytes() == b"nope"
        filt = next(h for _u, p, h, _c, _t in api if p.get("view") == "kona_player_info")
        assert json.loads(filt["X-Fantasy-Filter"])["players"]["limit"] == 2000
        assert all(c == {"SWID": "{A}", "espn_s2": "s"} and t == 30 for _u, _p, _h, c, t in api)

        assert m["live_source"] == "running watch"
        assert (root / "live" / "init.b64").read_text() == b64
        init = json.loads((root / "live" / "init.json").read_text())
        assert init["league_id"] == 1734659820
        picks = json.loads((root / "live" / "picks.json").read_text())
        assert len(picks) == len(espn_live.picks_from_init(espn_live.decode_init(b64)))
        assert all("draft_slot" in p for p in picks)
        logged = [json.loads(ln) for ln in (root / "live" / "lines.jsonl").read_text().splitlines()]
        assert logged == [{"ms": 1000, "line": lines[0][1]}, {"ms": 2000, "line": lines[1][1]}]
        assert json.loads((root / "manifest.json").read_text())["root"] == str(root)

    def test_opens_the_socket_once_without_a_watch(self, api, tmp_path, monkeypatch):
        b64 = FIXTURE.read_text().strip()
        monkeypatch.setattr(espn_live, "fetch_init_b64",
                            lambda *_a, **_k: (b64, ["SELECTED 3 4362628 4"]))
        m = espn_dump.dump_draft("1734659820", tmp_path, 2026, swid="{A}", espn_s2="s",
                                 team_id=3)
        assert m["live_source"] == "fresh socket snapshot"
        logged = [json.loads(ln) for ln in
                  (Path(m["root"]) / "live" / "lines.jsonl").read_text().splitlines()]
        assert logged == [{"ms": m["taken_at_ms"], "line": "SELECTED 3 4362628 4"}]
        assert len(api) == len(espn_dump.READ_VIEWS) + 2

    def test_no_live_section_without_watch_or_team(self, api, tmp_path):
        m = espn_dump.dump_draft("1734659820", tmp_path, 2026, swid="{A}", espn_s2="s")
        assert m["live_source"].startswith("none")
        assert len(api) == len(espn_dump.READ_VIEWS) + 2
        assert not (Path(m["root"]) / "live" / "init.json").exists()

    def test_requires_cookies(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ESPN_SWID", raising=False)
        monkeypatch.delenv("ESPN_S2", raising=False)
        with pytest.raises(RuntimeError):
            espn_dump.dump_draft("1", tmp_path, 2026)
