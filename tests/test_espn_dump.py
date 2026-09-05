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


def blind_draft_detail(rows: int = 224) -> dict:
    """mDraftDetail as ESPN really answers it mid-draft: every slot in the draft
    order, `playerId` -1 in all of them. Measured on a live dump at pick 130:
    224 rows, 0 filled."""
    return {"draftDetail": {"drafted": False, "inProgress": True,
                            "picks": [{"overallPickNumber": i, "playerId": -1, "teamId": 0}
                                      for i in range(1, rows + 1)]}}


def filled_draft_detail(picks: list[dict]) -> dict:
    """mDraftDetail as it reads once the draft has completed, built from the
    live rows so a matching pair is a matching pair by construction."""
    return {"draftDetail": {"drafted": True, "inProgress": False,
                            "picks": [{"overallPickNumber": p["overall"],
                                       "playerId": p["player_id"],
                                       "teamId": p["team_id"]} for p in picks]}}


class Calls(list):
    """The calls made, carrying the mDraftDetail body a test wants answered."""

    draft_detail: dict


@pytest.fixture
def api(monkeypatch):
    """Calls made, plus `api.draft_detail` to swap what mDraftDetail answers."""
    calls = Calls()
    calls.draft_detail = blind_draft_detail()

    def fake_get(url, params, cookies, headers, timeout):
        calls.append((url, params, headers, cookies, timeout))
        view = params.get("view")
        if view == "mStatus":
            return Resp(url, status=404, body=None)
        if view == "mDraftDetail":
            return Resp(url, body=calls.draft_detail)
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


def _dump(tmp_path, lines, **kwargs):
    b64 = FIXTURE.read_text().strip()
    m = espn_dump.dump_draft("1734659820", tmp_path, 2026, swid="{A}", espn_s2="s",
                             init_b64=b64, lines=lines, **kwargs)
    root = Path(m["root"])
    return m, root, {e["file"]: e for e in m["live"]}


def _read(root, name):
    return json.loads((root / "live" / name).read_text())


class TestCurrentState:
    """The defect: at pick 130 the dump's live section was the join snapshot's
    122 picks, and the eight SELECTED lines since sat unparsed in lines.jsonl."""

    def test_state_is_the_join_snapshot_plus_the_events_since(self, api, tmp_path):
        joined = len(espn_live.picks_from_init(espn_live.decode_init(FIXTURE.read_text().strip())))
        events = ["SELECTED 10 -16001 16", "SELECTED 3 4362628 4", "SELECTED 7 3000001 2"]
        m, root, live = _dump(tmp_path, [(1000 + i, ln) for i, ln in enumerate(events)])

        state = _read(root, "state.json")
        assert len(state) == joined + len(events)
        assert [p["overall"] for p in state] == list(range(1, joined + len(events) + 1))
        assert [p["source"] for p in state[-len(events):]] == ["selected"] * len(events)
        assert state[-1]["player_id"] == 3000001 and state[-1]["team_id"] == 7
        # The negative id is a team defense, not a parse failure.
        assert state[joined]["player_id"] == -16001

        # The join snapshot keeps its own number and does not move.
        assert len(_read(root, "picks.json")) == joined
        assert live["picks.json"] == {"file": "picks.json", "as_of": "join", "picks": joined}
        assert live["init.json"] == {"file": "init.json", "as_of": "join", "picks": joined}
        assert live["state.json"] == {"file": "state.json", "as_of": "now",
                                      "picks": joined + len(events), "picks_at_join": joined,
                                      "events_applied": len(events), "events_unparsed": 0}
        assert live["lines.jsonl"]["as_of"] == "now"

    def test_an_undone_pick_leaves_the_state_and_the_snapshot_alone(self, api, tmp_path):
        joined = len(espn_live.picks_from_init(espn_live.decode_init(FIXTURE.read_text().strip())))
        m, root, live = _dump(tmp_path, [(1, "SELECTED 10 -16001 16"),
                                         (2, f"UNDONE {joined}"),
                                         (3, "SELECTED 3 4362628 4")])

        state = _read(root, "state.json")
        # The rolled-back pick is gone and the replacement took its number back.
        assert len(state) == joined + 1
        assert state[-1]["player_id"] == 4362628
        assert len(_read(root, "picks.json")) == joined

    def test_the_pick_queue_is_materialised_without_moving_a_pick(self, api, tmp_path):
        """DRAFT_LIST is the queue echo. It is live state that exists nowhere but
        the event log, and it changes nobody's picks."""
        joined = len(espn_live.picks_from_init(espn_live.decode_init(FIXTURE.read_text().strip())))
        m, root, live = _dump(tmp_path, [(1, "DRAFT_LIST 3916433 4569587"),
                                         (2, "SELECTED 3 3916433 10 {A}"),
                                         (3, "DRAFT_LIST 4569587 -16034")])

        assert _read(root, "queue.json")["queue"] == [4569587, -16034]
        assert live["queue.json"] == {"file": "queue.json", "as_of": "now",
                                      "echoes": 2, "players": 2}
        # One SELECTED among three lines: the two DRAFT_LISTs moved nothing.
        assert len(_read(root, "state.json")) == joined + 1
        assert live["state.json"]["events_applied"] == 1

    def test_a_queue_espn_never_echoed_is_unknown_not_empty(self, api, tmp_path):
        m, root, live = _dump(tmp_path, [(1, "SELECTED 10 -16001 16")])

        queue = _read(root, "queue.json")
        assert queue["queue"] is None and queue["echoes"] == 0
        assert "not the same as empty" in queue["note"]
        assert live["queue.json"]["players"] is None

    def test_an_unparsable_pick_event_is_counted_not_dropped(self, api, tmp_path):
        m, root, live = _dump(tmp_path, [(1, "SELECTED 10 -16001 16"),
                                         (2, "SELECTED whoever 4362628 4")])

        assert live["state.json"]["events_applied"] == 1
        assert live["state.json"]["events_unparsed"] == 1
        assert any("could not be parsed" in e for e in m["errors"])


class TestReconcile:
    def test_a_blind_read_api_is_not_a_mismatch(self, api, tmp_path):
        """Mid-draft ESPN answers mDraftDetail with every slot at playerId -1.
        Reporting that as 117 missing picks would fire on every live dump."""
        m, root, live = _dump(tmp_path, [(1, "SELECTED 10 -16001 16")])

        report = _read(root, "reconcile.json")
        assert report["status"] == "blind"
        assert report["read_api_rows"] == 224 and report["read_api_picks"] == 0
        assert report["live_picks"] == len(_read(root, "state.json"))
        assert m["reconcile"]["status"] == "blind"
        assert live["reconcile.json"]["status"] == "blind"
        assert m["errors"] == ["mStatus: HTTP 404"]

    def test_a_matching_draft_detail_reconciles_clean(self, api, tmp_path):
        b64 = FIXTURE.read_text().strip()
        events = ["SELECTED 10 -16001 16", "SELECTED 3 4362628 4"]
        state = espn_live.replay_picks(espn_live.decode_init(b64), events)
        api.draft_detail = filled_draft_detail(state)

        m, root, live = _dump(tmp_path, list(enumerate(events)))

        report = _read(root, "reconcile.json")
        assert report["status"] == "clean"
        assert report["live_picks"] == report["read_api_picks"] == len(state)
        assert report["missing_from_read_api"] == [] and report["missing_from_live"] == []
        assert m["errors"] == ["mStatus: HTTP 404"]

    def test_a_disagreement_is_reported_not_swallowed(self, api, tmp_path):
        b64 = FIXTURE.read_text().strip()
        events = ["SELECTED 10 -16001 16", "SELECTED 3 4362628 4"]
        state = espn_live.replay_picks(espn_live.decode_init(b64), events)
        detail = filled_draft_detail(state)
        picks = detail["draftDetail"]["picks"]
        picks[0]["playerId"] = 999999          # same pick, a different player
        dropped = picks.pop()["overallPickNumber"]  # a pick the read API never got
        extra = state[-1]["overall"] + 1
        picks.append({"overallPickNumber": extra, "playerId": 111, "teamId": 2})
        api.draft_detail = detail

        m, root, live = _dump(tmp_path, list(enumerate(events)))

        report = _read(root, "reconcile.json")
        assert report["status"] == "mismatch"
        assert report["missing_from_read_api"] == [dropped]
        assert report["missing_from_live"] == [extra]
        assert [d["overall"] for d in report["disagreements"]] == [1]
        assert report["disagreements"][0]["read_api"]["player_id"] == 999999
        assert report["disagreements"][0]["live"]["player_id"] == state[0]["player_id"]
        assert "reconcile against mDraftDetail: mismatch" in m["errors"]
        assert m["reconcile"]["status"] == "mismatch"
        # One pick each way plus one disagreement: readable from the manifest
        # alone, without opening reconcile.json.
        assert m["reconcile"]["differences"] == 3

    def test_a_response_without_draft_detail_is_unreadable_not_clean(self, api, tmp_path):
        api.draft_detail = {"view": "mDraftDetail", "id": 1734659820}

        m, root, live = _dump(tmp_path, [(1, "SELECTED 10 -16001 16")])

        assert _read(root, "reconcile.json")["status"] == "unreadable"
        assert "reconcile against mDraftDetail: unreadable" in m["errors"]

    def test_no_live_state_says_so(self, api, tmp_path):
        m = espn_dump.dump_draft("1734659820", tmp_path, 2026, swid="{A}", espn_s2="s")
        assert m["reconcile"] == {"status": "no live state"}
        assert not (Path(m["root"]) / "live" / "reconcile.json").exists()
