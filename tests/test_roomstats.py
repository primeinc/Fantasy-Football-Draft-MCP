import datetime as dt
import json
from typing import Any

import pytest

from ffdraft import board as bd
from ffdraft import roomstats

# A fixed local wall-clock start, so the hour histogram is the same wherever
# this runs: the report reads local time on purpose.
T0 = int(dt.datetime(2026, 9, 5, 9, 0, 0).timestamp() * 1000)
MINUTE = 60_000
SWID_A = "{AAAAAAAA-0000-0000-0000-000000000001}"
SWID_B = "{BBBBBBBB-0000-0000-0000-000000000002}"


def mteam():
    return {
        "members": [
            {"id": SWID_A, "firstName": "Ada", "lastName": "Lovelace", "displayName": "ESPNFAN1"},
            {"id": SWID_B, "firstName": "Bo", "lastName": "Jackson", "displayName": "ESPNFAN2"},
        ],
        "teams": [
            {"id": 1, "name": "Analytical Engines", "owners": [SWID_A]},
            {"id": 2, "name": "Two Sport Stars", "owners": [SWID_B]},
            {"id": 3, "name": "Nobody Home", "owners": []},
        ],
    }


def synthetic_lines():
    """One draft: team 1 is already in the room, team 2 joins, both pick, team 2
    leaves and comes back, and both say something."""
    return [
        (T0, "INIT xxxx"),
        (T0, f"CHAT 1 {SWID_A} {T0 - 5 * MINUTE} good+morning"),
        (T0 + 1 * MINUTE, "JOINED 2 0"),
        (T0 + 2 * MINUTE, "SELECTED 1 100"),           # not timed: first after INIT
        (T0 + 2 * MINUTE + 30_000, "SELECTED 2 200"),  # 30 s on the clock
        (T0 + 3 * MINUTE, f"CHAT 2 {SWID_B} {T0 + 3 * MINUTE} nice+pick%21"),
        (T0 + 4 * MINUTE, "SELECTED 2 201"),           # 90 s on the clock
        (T0 + 5 * MINUTE, f"LEFT 2 {SWID_B} 0"),
        (T0 + 7 * MINUTE, "JOINED 2 0"),
        (T0 + 8 * MINUTE, "SELECTED 1 101"),           # 240 s on the clock
        (T0 + 10 * MINUTE, "UNDONE 3"),
        (T0 + 12 * MINUTE, "SELECTED 1 102"),          # not timed: follows UNDONE
    ]


def make_log(**kw):
    base: dict[str, Any] = {
        "lines": synthetic_lines(),
        "online_at_start": {1: True, 2: False, 3: False},
        "directory": bd.league_directory_from_mteam(mteam()),
        "member_names": bd.mteam_member_names(mteam()),
        "source": "test",
    }
    base.update(kw)
    return roomstats.RoomLog(**base)


def by_team(stats):
    return {m["team_id"]: m for m in stats["members"]}


class TestPresence:
    def test_minutes_span_joins_leaves_and_the_still_open_session(self):
        m = by_team(roomstats.room_stats(make_log()))
        # Team 1 was online at the first line and never left: the whole window.
        assert m[1]["minutes_in_room"] == 12.0
        assert m[1]["in_room_at_start"] is True
        assert m[1]["in_room_at_end"] is True
        assert m[1]["joins"] == 0
        # Team 2: minute 1 to 5, then 7 to the end of the log.
        assert m[2]["minutes_in_room"] == 9.0
        assert m[2]["joins"] == 2
        assert m[2]["leaves"] == 1
        assert [s["minutes"] for s in m[2]["sessions"]] == [4.0, 5.0]
        assert m[2]["in_room_at_start"] is False

    def test_a_team_that_never_appears_is_still_listed_at_zero(self):
        m = by_team(roomstats.room_stats(make_log()))
        assert m[3]["minutes_in_room"] == 0.0
        assert m[3]["sessions"] == []
        assert m[3]["first_seen"] is None

    def test_members_are_ordered_by_time_in_the_room(self):
        stats = roomstats.room_stats(make_log())
        assert [m["team_id"] for m in stats["members"]] == [1, 2, 3]

    def test_a_replaced_connection_does_not_end_our_own_session(self):
        # `LEFT <team> <swid> 2` is ESPN closing this connection because the
        # same team opened the room elsewhere. watch.py treats it as a pause,
        # not a departure — and it is the reader's own team, sitting there
        # drafting for the rest of the night.
        lines = [(T0, "INIT x"), (T0 + 5 * MINUTE, f"LEFT 1 {SWID_A} 2"),
                 (T0 + 20 * MINUTE, "SELECTED 1 5")]
        m = by_team(roomstats.room_stats(make_log(lines=lines, own_team_id=1)))
        assert m[1]["minutes_in_room"] == 20.0
        assert m[1]["leaves"] == 0
        assert m[1]["connections_replaced"] == 1
        assert m[1]["in_room_at_end"] is True

    def test_another_team_s_replaced_connection_is_a_leave_that_says_why(self):
        lines = [(T0, "INIT x"), (T0 + 5 * MINUTE, f"LEFT 2 {SWID_B} 2"),
                 (T0 + 20 * MINUTE, "SELECTED 1 5")]
        m = by_team(roomstats.room_stats(
            make_log(lines=lines, own_team_id=1, online_at_start={1: True, 2: True})))
        assert m[2]["minutes_in_room"] == 5.0
        assert m[2]["leaves"] == 1
        assert m[2]["sessions"][0]["ended_by"] == "connection_replaced"
        assert m[1]["sessions"][0]["ended_by"] == "log_end"


class TestChat:
    def test_chat_uses_the_line_s_own_send_time_not_the_receive_time(self):
        stats = roomstats.room_stats(make_log())
        m = by_team(stats)
        assert m[1]["messages"] == 1
        assert m[1]["last_message"] == "good morning"
        # Replayed on join at T0, but sent five minutes earlier: 08:55.
        assert m[1]["first_seen"].endswith("08:55:00")
        assert m[2]["last_message"] == "nice pick!"
        assert stats["totals"]["messages"] == 2

    def test_messages_are_attributed_to_the_owner_by_name(self):
        m = by_team(roomstats.room_stats(make_log()))
        assert m[1]["messages_by_owner"] == {"Ada Lovelace": 1}
        assert m[2]["messages_by_owner"] == {"Bo Jackson": 1}

    def test_an_unknown_swid_falls_back_to_the_team_s_only_owner(self):
        lines = [(T0, "INIT x"),
                 (T0, f"CHAT 1 {{DEADBEEF-0000-0000-0000-000000000009}} {T0} hello")]
        stats = roomstats.room_stats(make_log(lines=lines))
        blob = json.dumps(stats) + roomstats.format_table(stats)
        assert "DEADBEEF" not in blob
        assert by_team(stats)[1]["messages_by_owner"] == {"Ada Lovelace": 1}

    def test_an_unknown_swid_on_a_team_with_no_owner_is_labelled(self):
        lines = [(T0, "INIT x"),
                 (T0, f"CHAT 3 {{DEADBEEF-0000-0000-0000-000000000009}} {T0} hello")]
        stats = roomstats.room_stats(make_log(lines=lines))
        assert by_team(stats)[3]["messages_by_owner"] == {roomstats.UNKNOWN_LABEL: 1}
        assert "DEADBEEF" not in json.dumps(stats)

    def test_no_swid_appears_anywhere_in_the_output(self):
        stats = roomstats.room_stats(make_log())
        blob = json.dumps(stats) + roomstats.format_table(stats)
        for swid in (SWID_A, SWID_B):
            assert swid not in blob
            assert swid.strip("{}") not in blob


class TestClock:
    def test_time_on_the_clock_is_the_gap_between_selections(self):
        m = by_team(roomstats.room_stats(make_log()))
        # Team 2's two picks: 30 s and 90 s after the pick before each.
        assert m[2]["clock_to_pick"]["n"] == 2
        assert m[2]["clock_to_pick"]["median_seconds"] == 60.0
        assert m[2]["clock_to_pick"]["fastest_seconds"] == 30.0
        # Team 1: the pick after INIT has no start and the one after UNDONE is
        # dropped, so only the 240 s pick is timed.
        assert m[1]["picks"] == 3
        assert m[1]["clock_to_pick"]["n"] == 1
        assert m[1]["clock_to_pick"]["median_seconds"] == 240.0
        assert roomstats.room_stats(make_log())["totals"]["clock_to_pick"]["n"] == 3

    def test_a_pause_is_kept_out_of_the_median(self):
        pause = int(roomstats.PICK_GAP_CAP_SECONDS * 1000) + 60_000
        lines = [(T0, "INIT x"), (T0, "SELECTED 1 1"), (T0 + 20_000, "SELECTED 1 2"),
                 (T0 + 20_000 + pause, "SELECTED 1 3")]
        clock = by_team(roomstats.room_stats(make_log(lines=lines)))[1]["clock_to_pick"]
        assert clock["n"] == 2
        assert clock["n_timed"] == 1
        assert clock["median_seconds"] == 20.0
        assert clock["slowest_seconds"] == round(pause / 1000, 1)

    def test_selecting_dates_the_clock_instead_of_the_previous_pick(self):
        # SELECTING names the team on the clock, so nothing has to be inferred
        # about the moment between a pick landing and the next clock starting.
        lines = [(T0, "INIT x"),
                 (T0, "SELECTING 1 90"),
                 (T0 + 45_000, "SELECTED 1 1"),
                 (T0 + 50_000, "SELECTING 2 90"),
                 (T0 + 60_000, "SELECTED 2 2")]
        m = by_team(roomstats.room_stats(make_log(lines=lines)))
        # Timed from SELECTING, not from INIT or the previous SELECTED.
        assert m[1]["clock_to_pick"]["median_seconds"] == 45.0
        assert m[1]["clock_to_pick"]["measured_by"] == {"selecting": 1}
        assert m[2]["clock_to_pick"]["median_seconds"] == 10.0

    def test_the_selected_gap_is_the_fallback_and_says_so(self):
        clock = by_team(roomstats.room_stats(make_log()))[2]["clock_to_pick"]
        assert clock["measured_by"] == {"selected_gap": 2}

    def test_no_picks_means_no_clock_block(self):
        stats = roomstats.room_stats(make_log(lines=[(T0, "INIT x"), (T0, "JOINED 2 0")]))
        assert by_team(stats)[2]["clock_to_pick"] is None
        assert stats["totals"]["clock_to_pick"] is None


class TestHoursAndActivity:
    def test_busiest_hours_count_room_events_and_league_activity(self):
        activity = [(T0 + 6 * 3_600_000 + n * MINUTE, SWID_A) for n in range(4)]
        m = by_team(roomstats.room_stats(make_log(activity=activity)))
        # Three picks in hour 09 and a chat in hour 08, against four topics in 15.
        assert m[1]["top_hours"] == ["15:00", "09:00", "08:00"]
        assert m[1]["active_hours"] == {"08": 1, "09": 3, "15": 4}
        assert m[1]["league_activity"]["count"] == 4
        # Activity does not move room first/last seen.
        assert m[1]["last_seen"].endswith("09:12:00")

    def test_activity_by_an_author_with_no_team_is_counted_not_attributed(self):
        stats = roomstats.room_stats(
            make_log(activity=[(T0, "{CCCCCCCC-0000-0000-0000-000000000003}")]))
        assert stats["totals"]["league_activity_topics"] == 1
        assert stats["totals"]["league_activity_unmatched"] == 1
        assert all(m["league_activity"]["count"] == 0 for m in stats["members"])

    def test_an_empty_log_reports_the_roster_and_no_window(self):
        stats = roomstats.room_stats(make_log(lines=[]))
        assert stats["window"] == {"from": None, "to": None, "minutes": None, "lines": 0}
        assert stats["totals"]["members"] == 3
        assert stats["totals"]["messages"] == 0
        assert "member" in roomstats.format_table(stats)


class TestSources:
    def dump(self, root, lines=None):
        (root / "live").mkdir(parents=True)
        (root / "read_api").mkdir(parents=True)
        with (root / "live" / "lines.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
            for ms, line in (synthetic_lines() if lines is None else lines):
                fh.write(json.dumps({"ms": ms, "line": line}) + "\n")
        (root / "live" / "init.json").write_text(json.dumps(
            {"team_id": 1, "league": {"draft_teams": [
                {"team_id": 1, "owners": [{"is_online": True}]},
                {"team_id": 2, "owners": [{"is_online": False}, None]},
                None,
            ]}}), encoding="utf-8")
        (root / "read_api" / "mTeam.json").write_text(json.dumps(mteam()), encoding="utf-8")
        (root / "read_api" / "kona_league_communication.json").write_text(json.dumps(
            {"communication": {"topics": [
                {"author": SWID_A, "date": T0, "type": "ACTIVITY_SETTINGS"},
                {"date": T0, "type": "ACTIVITY_STATUS"},
            ]}}), encoding="utf-8")
        return root

    def test_from_dump_reads_lines_presence_members_and_activity(self, tmp_path):
        root = self.dump(tmp_path / "espn_dump_1_2026_x")
        log = roomstats.from_dump(root)
        assert log.source == "dump espn_dump_1_2026_x"
        assert log.online_at_start == {1: True, 2: False}
        # The INIT payload names the team whose connection produced the log, so
        # its own replaced connections can be told from real departures.
        assert log.own_team_id == 1
        assert log.activity == [(T0, SWID_A)]
        m = by_team(roomstats.room_stats(log))
        assert m[1]["minutes_in_room"] == 12.0
        assert m[1]["owners"] == ["Ada Lovelace"]
        assert m[1]["league_activity"]["count"] == 1
        assert m[2]["clock_to_pick"]["median_seconds"] == 60.0

    def test_find_dump_takes_the_newest_and_accepts_one_directly(self, tmp_path):
        self.dump(tmp_path / "espn_dump_1_2026_20260901-000000")
        newest = self.dump(tmp_path / "espn_dump_1_2026_20260905-000000")
        assert roomstats.find_dump(tmp_path) == newest
        assert roomstats.find_dump(newest) == newest

    def test_find_dump_orders_by_stamp_not_by_league_id(self, tmp_path):
        # Sorting the whole directory name orders by league id first, so with
        # two leagues side by side "newest" became the bigger number.
        newest = self.dump(tmp_path / "espn_dump_111_2026_20260905-000000")
        self.dump(tmp_path / "espn_dump_999_2026_20260901-000000")
        assert roomstats.find_dump(tmp_path) == newest

    def test_from_dump_survives_a_directory_with_only_the_read_api(self, tmp_path):
        root = tmp_path / "espn_dump_2_2026_y"
        (root / "read_api").mkdir(parents=True)
        (root / "read_api" / "mTeam.json").write_text(json.dumps(mteam()), encoding="utf-8")
        stats = roomstats.room_stats(roomstats.from_dump(root))
        assert stats["totals"]["members"] == 3
        assert stats["window"]["lines"] == 0

    def test_from_dump_rejects_a_missing_directory(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            roomstats.from_dump(tmp_path / "nope")
        assert roomstats.find_dump(tmp_path) is None

    def watch(self, **kw):
        class FakeWatch:
            lines = synthetic_lines()
            # Mutated live by every JOINED/LEFT, so by report time team 2 reads
            # as present even though it arrived a minute in.
            online = {1: True, 2: True, 3: False}
            online_at_init = {1: True, 2: False, 3: False}
            directory: dict = bd.league_directory_from_mteam(mteam())
            presence: list = []
            chat: list = []
            team_id = None

        for key, value in kw.items():
            setattr(FakeWatch, key, value)
        return FakeWatch()

    def test_from_watch_reads_the_watch_s_lines_and_directory(self):
        log = roomstats.from_watch(self.watch())
        assert log.source == "watch"
        m = by_team(roomstats.room_stats(log))
        assert m[1]["minutes_in_room"] == 12.0
        assert m[2]["messages_by_owner"] == {"Bo Jackson": 1}

    def test_presence_uses_the_init_snapshot_not_the_live_online_dict(self):
        # DraftWatch.online is mutated by every JOINED and LEFT, so by report
        # time it says who is in the room NOW. Seeding from it would credit
        # team 2 with the whole window instead of the eleven minutes it was
        # there, and the ranking is built on that number.
        m = by_team(roomstats.room_stats(roomstats.from_watch(self.watch())))
        assert m[2]["minutes_in_room"] == 9.0
        assert m[2]["in_room_at_start"] is False
        assert m[2]["sessions"][0]["from"].endswith("09:01:00")

    def test_a_watch_without_the_init_snapshot_seeds_nothing(self):
        watch = self.watch()
        del type(watch).online_at_init
        m = by_team(roomstats.room_stats(roomstats.from_watch(watch)))
        # Under-counting team 1 is the safe failure; back-crediting is not.
        assert m[1]["minutes_in_room"] == 0.0
        assert m[2]["minutes_in_room"] == 9.0

    def test_from_watch_falls_back_to_presence_and_chat(self):
        watch = self.watch(lines=[], online_at_init={1: False},
                           presence=[(T0, 1, "joined"), (T0 + 6 * MINUTE, 1, "left")],
                           chat=[(T0 + MINUTE, 1, SWID_A, "100% sure")])
        m = by_team(roomstats.room_stats(roomstats.from_watch(watch)))
        assert m[1]["minutes_in_room"] == 6.0
        assert m[1]["joins"] == 1
        # watch.chat text is already decoded; re-encoding on the way out keeps
        # the parser's decode from eating a literal percent escape.
        assert m[1]["last_message"] == "100% sure"


class TestTable:
    def test_the_table_names_people_and_carries_the_numbers(self):
        stats = roomstats.room_stats(make_log())
        text = roomstats.format_table(stats)
        rows = [ln for ln in text.splitlines() if ln.strip()]
        assert rows[0].startswith("ESPN draft room")
        assert "Ada Lovelace" in text and "Bo Jackson" in text
        assert "Analytical Engines" in text
        ada = next(r for r in rows if r.startswith("Ada"))
        assert "12.0" in ada and "240" in ada
        assert all(len(ln) < 200 for ln in text.splitlines())

    def test_a_snapshot_with_no_watch_says_so(self):
        stats = roomstats.room_stats(make_log(lines=[(T0, "INIT x"), (T0, "JOINED 2 0")]))
        assert "No watch was running" in roomstats.format_table(stats)


class TestDirectory:
    def test_the_mteam_split_matches_what_the_live_view_produced(self):
        assert bd.league_directory_from_mteam(mteam())[1] == {
            "name": "Analytical Engines", "owners": ["Ada Lovelace"],
            "owner_ids": [SWID_A.strip("{}")]}
        assert bd.mteam_member_names(mteam())[SWID_A.strip("{}")] == "Ada Lovelace"

    def test_a_team_without_a_name_falls_back_to_location_and_nickname(self):
        data = {"members": [], "teams": [{"id": 4, "location": "Big", "nickname": "Deal",
                                          "owners": []}]}
        assert bd.league_directory_from_mteam(data)[4]["name"] == "Big Deal"

    def test_an_owner_the_member_list_does_not_name_is_never_his_swid(self):
        # The fallback used to be the SWID itself, which reaches every report
        # and every pushed channel message through DraftWatch.team_label.
        data = {"members": [], "teams": [{"id": 5, "name": "Orphans", "owners": [SWID_B]}]}
        entry = bd.league_directory_from_mteam(data)[5]
        assert entry["owners"] == [bd.UNKNOWN_OWNER]
        assert entry["owner_ids"] == [SWID_B.strip("{}")]

    def test_an_unnamed_owner_keeps_his_swid_out_of_the_whole_report(self):
        data = {"members": [{"id": SWID_A, "firstName": "Ada", "lastName": "Lovelace"}],
                "teams": [{"id": 1, "name": "Analytical Engines", "owners": [SWID_A]},
                          {"id": 2, "name": "Orphans", "owners": [SWID_B]}]}
        log = roomstats.RoomLog(
            lines=synthetic_lines(), online_at_start={1: True, 2: False},
            directory=bd.league_directory_from_mteam(data),
            member_names=bd.mteam_member_names(data), source="test")
        stats = roomstats.room_stats(log)
        blob = json.dumps(stats) + roomstats.format_table(stats)
        assert SWID_B not in blob and SWID_B.strip("{}") not in blob
        assert by_team(stats)[2]["owners"] == [bd.UNKNOWN_OWNER]
        assert by_team(stats)[2]["messages_by_owner"] == {bd.UNKNOWN_OWNER: 1}
