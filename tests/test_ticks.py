"""`ticks.delta`: the heartbeat's "report only deltas", computed from stored state."""
from ffdraft import ticks

BASE = {"statuses": {"Kyler Murray": "QUESTIONABLE"}, "disagreements": [],
        "versus_espn": {"start": [], "bench": [], "gain": 0}, "next_lock": None, "unread": {},
        "newest_feed_entry": {"player": "Jordan Addison", "as_of": "2026-09-14T01:13Z"}}


def test_an_unchanged_tick_has_no_delta():
    assert ticks.delta(BASE, dict(BASE)) == []


def test_the_first_tick_reports_what_is_there():
    assert ticks.delta(None, BASE) == ["Kyler Murray: ACTIVE -> QUESTIONABLE"]


def test_a_status_change_carries_the_feed_as_of_when_the_feed_is_about_him():
    cur = {**BASE, "statuses": {"Kyler Murray": "OUT"},
           "newest_feed_entry": {"player": "Kyler Murray", "as_of": "2026-09-19T15:00Z"}}
    assert ticks.delta(BASE, cur) == [
        "Kyler Murray: QUESTIONABLE -> OUT (feed as_of 2026-09-19T15:00Z)"]
    assert ticks.delta(cur, {**BASE, "statuses": {}}) == ["Kyler Murray: OUT -> ACTIVE"]


def test_a_new_disagreement_lock_gain_and_unread_are_reported_once():
    cur = {**BASE,
           "disagreements": [{"player": "Tracy", "roster": "ACTIVE", "feed": "OUT", "as_of": "t"}],
           "versus_espn": {"start": ["A"], "bench": ["Tracy"], "gain": 4.2},
           "next_lock": {"kickoff": "2026-09-20 13:00 ET", "players": ["Tracy"]},
           "unread": {"schedule": "boom"}}
    assert ticks.delta(BASE, cur) == [
        "disagreement: Tracy roster ACTIVE, feed OUT as_of t",
        "lineup gain 4.2: start ['A'], bench ['Tracy']",
        "next lock 2026-09-20 13:00 ET: Tracy",
        "unreadable: schedule: boom"]
    assert ticks.delta(cur, dict(cur)) == []


def test_a_gain_with_no_lock_ahead_is_not_reported():
    cur = {**BASE, "versus_espn": {"start": ["A"], "bench": ["B"], "gain": 3.0}}
    assert ticks.delta(BASE, cur) == []


def test_the_state_file_round_trips(tmp_path):
    path = ticks.path_for("123", 1, tmp_path)
    assert ticks.load(path) is None
    ticks.save(BASE, path)
    assert ticks.load(path) == BASE
    path.write_text("{", encoding="utf-8")
    assert ticks.load(path) is None
