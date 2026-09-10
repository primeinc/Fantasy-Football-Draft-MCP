"""`game_tick`: the delta a five-minute loop reads, with every part's basis.

Built because the loop's decision-time probe had become a `python -c`
one-liner over the helpers -- the tell the decision-surface invariant names.
"""
import json

from test_weekly_lineup import _entries, _wire

from ffdraft import injuries, server, sources


def _feed():
    return {"timestamp": "2026-09-10T00:46:16Z", "injuries": [
        {"id": "30", "injuries": [
            {"status": "Questionable", "date": "2026-09-09T16:28Z",
             "athlete": {"displayName": "WR One"},
             "shortComment": "non-contact jersey",
             "details": {"type": "Thumb",
                         "fantasyStatus": {"abbreviation": "QUESTIONABLE"}}}]}]}


def _tick(monkeypatch, entries=None):
    _wire(monkeypatch, entries=entries)
    monkeypatch.setattr(injuries, "fetch_injuries", lambda *a, **k: _feed())
    return json.loads(server.game_tick("123", 9))


def test_a_quiet_week_reads_as_quiet_with_every_basis_named(monkeypatch):
    out = _tick(monkeypatch)
    assert out["versus_espn"]["start"] == [] and out["versus_espn"]["bench"] == []
    assert out["locked"] == []
    assert out["statuses"] == {}
    assert out["feed_timestamp"] == "2026-09-10T00:46:16Z"
    assert out["unread"] == {}
    assert out["next_lock"]["kickoff"] == "2026-11-01 13:00 ET"
    assert "RB One" in out["next_lock"]["players"]
    assert out["status_basis"] and out["next_lock_basis"]


def test_a_roster_that_lags_the_feed_is_a_disagreement(monkeypatch):
    # WR One is QUESTIONABLE in the feed and ACTIVE on the roster: the row to
    # act on. The fixture roster's team is not JAX, so the join is by name.
    out = _tick(monkeypatch)
    assert [d["player"] for d in out["disagreements"]] == ["WR One"]
    assert out["disagreements"][0]["as_of"] == "2026-09-09T16:28Z"
    assert out["newest_feed_entry"]["player"] == "WR One"


def test_an_unreadable_feed_or_schedule_is_named_not_filled(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(injuries, "fetch_injuries",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("403")))
    monkeypatch.setattr(sources, "schedules",
                        lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    out = json.loads(server.game_tick("123", 9))
    assert out["feed_timestamp"] is None and out["disagreements"] == []
    assert out["next_lock"] is None
    assert set(out["unread"]) == {"injury_feed", "schedule"}


def test_locked_players_are_listed_and_leave_the_next_lock(monkeypatch):
    entries = _entries()
    for e in entries:
        if e["playerPoolEntry"]["player"]["fullName"] == "RB One":
            e["playerPoolEntry"]["lineupLocked"] = True
    out = _tick(monkeypatch, entries=entries)
    assert out["locked"] == ["RB One"]
    assert "RB One" not in out["next_lock"]["players"]
