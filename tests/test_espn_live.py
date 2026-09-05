"""ESPN live-draft INIT decoder, tested offline against a captured payload."""
import base64
from pathlib import Path

import pytest

from ffdraft import espn_live
from ffdraft.espn_live import Reader, decode_init, picks_from_init

FIXTURE = Path(__file__).parent / "fixtures" / "espn_draft_init.b64"


@pytest.fixture(scope="module")
def payload() -> str:
    return FIXTURE.read_text().strip()


@pytest.fixture(scope="module")
def init(payload):
    return decode_init(payload)


class TestReader:
    def test_read_int_is_big_endian_and_signed(self):
        assert Reader(bytes([0x00, 0x00, 0x00, 0x2A])).read_int() == 42
        assert Reader(bytes([0xFF, 0xFF, 0xFF, 0xFF])).read_int() == -1

    def test_read_int_keeps_exactly_two_to_the_31_positive(self):
        # The JS compares with `>`, not `>=`, so 0x80000000 does not wrap.
        assert Reader(bytes([0x80, 0x00, 0x00, 0x00])).read_int() == 2**31
        assert Reader(bytes([0x80, 0x00, 0x00, 0x01])).read_int() == -(2**31) + 1

    def test_short_long_boolean(self):
        assert Reader(bytes([0x01, 0x00])).read_short() == 256
        assert Reader(bytes([0, 0, 0, 0, 0, 0, 0x01, 0x00])).read_long() == 256
        assert Reader(bytes([1])).read_boolean() is True
        assert Reader(bytes([0])).read_boolean() is False
        assert Reader(bytes([2])).read_boolean() is False

    def test_read_utf_is_length_prefixed_latin1(self):
        r = Reader(bytes([0x00, 0x03]) + "abc".encode("latin-1"))
        assert r.read_utf() == "abc"
        assert r.remaining() == 0

    def test_double_and_float_consume_bytes_and_discard_the_value(self):
        r = Reader(bytes(12))
        assert r.read_double() is None
        assert r.index == 8
        assert r.read_float() is None
        assert r.index == 12

    def test_optional_date_reads_millis_only_when_marked(self):
        r = Reader(bytes(4))
        assert r.read_date() is None
        assert r.index == 4
        r = Reader(bytes([0, 0, 0, 1]) + (1788461100000).to_bytes(8, "big"))
        assert r.read_date() == 1788461100000
        assert r.remaining() == 0

    def test_read_past_the_end_raises(self):
        with pytest.raises(ValueError):
            Reader(bytes(3)).read_int()


class TestHeader:
    def test_absent_marker_yields_a_null_object_and_reads_nothing_more(self):
        r = Reader(bytes(4))
        assert espn_live._decode_draft_pick(r) is None
        assert r.index == 4

    def test_wrong_version_raises_naming_both_versions(self):
        r = Reader(bytes([0, 0, 0, 1]) + (9).to_bytes(4, "big"))
        with pytest.raises(ValueError) as exc:
            espn_live._decode_draft_pick(r)
        assert "9" in str(exc.value)
        assert "DraftPickStorableTranscoder" in str(exc.value)


class TestDecodeInit:
    def test_identifies_the_league_and_the_joining_team(self, init):
        assert init.league_id == 1734659820
        assert init.team_id == 3

    def test_consumes_the_whole_buffer_exactly(self, payload):
        reader = Reader(base64.b64decode(payload))
        espn_live._decode_draft_init(reader)
        assert reader.index == len(reader.data)
        assert reader.remaining() == 0

    def test_league_shape(self, init):
        league = init.league
        assert league is not None
        assert league.league_id == 1734659820
        assert league.draft_type == 1
        assert league.draft_state == 1
        assert league.draft_date == 1788461100000
        assert len(league.draft_teams) == 16
        assert len(league.draft_picks) == 224
        assert len(league.draft_positions) == 18
        assert len(league.draft_slots) == 14

    def test_nested_rules_and_scoring_decode(self, init):
        rules = init.league.draft_rules
        assert rules is not None
        assert rules.league_id == 1734659820
        assert rules.scoring_settings is not None
        assert len(rules.scoring_settings.scoring_categories) == 46

    def test_null_draft_list_decodes_to_none(self, init):
        # The presence marker is 0: this team has no custom draft list. The
        # exact-consumption test above proves no bytes were skipped for it.
        assert init.draft_list is None
        assert init.nomination_list is not None
        assert init.nomination_list.team_id == 3
        assert len(init.nomination_list.nomination_list_players) == 8


class TestPicksFromInit:
    def test_returns_only_picks_that_have_been_made(self, init):
        # 224 pick slots, 110 of them still carrying the -1 sentinel.
        unfilled = [p for p in init.league.draft_picks if p.player_id == -1]
        assert len(unfilled) == 110
        assert len(picks_from_init(init)) == 114

    def test_overall_numbers_are_contiguous_and_sorted(self, init):
        overalls = [row["overall"] for row in picks_from_init(init)]
        assert overalls == sorted(overalls)
        assert overalls == list(range(1, 115))

    def test_row_shape(self, init):
        row = picks_from_init(init)[0]
        assert set(row) == {"overall", "team_id", "player_id", "slot_id", "keeper"}
        assert isinstance(row["keeper"], bool)

    def test_known_picks(self, init):
        by_overall = {row["overall"]: row for row in picks_from_init(init)}
        # Jahmyr Gibbs at 1.01, Ja'Marr Chase to the joining team at 1.04.
        # Both ids confirmed against the nflverse espn_id crosswalk.
        assert by_overall[1]["player_id"] == 4429795
        assert by_overall[4]["team_id"] == 3
        assert by_overall[4]["player_id"] == 4362628

    def test_a_null_league_yields_no_picks(self):
        assert picks_from_init(espn_live.DraftInit()) == []


class TestDraftTeams:
    def test_joining_team_roster(self, init):
        team = next(t for t in init.league.draft_teams if t.team_id == 3)
        assert team.draft_position == 3
        assert len(team.owners) == 1
        # One roster item per roster slot; unfilled slots carry the -1 sentinel.
        assert len(team.draft_roster_items) == 14
        filled = [item for item in team.draft_roster_items if item.player_id > 0]
        assert len(filled) == 7

    def test_draft_position_is_zero_based(self, init):
        # Team 3 sits at draft_position 3 and picks 4th overall. Consumers that
        # render a draft slot must add one.
        first_round = {
            row["team_id"]: row["overall"] for row in picks_from_init(init) if row["overall"] <= 16
        }
        positions = sorted(
            (team.draft_position, first_round[team.team_id]) for team in init.league.draft_teams
        )
        assert positions == [(i, i + 1) for i in range(16)]

    def test_slot_by_team_is_one_based(self, init):
        slots = espn_live.slot_by_team(init)
        assert len(slots) == 16
        assert sorted(slots.values()) == list(range(1, 17))
        assert slots[3] == 4
        assert espn_live.slot_by_team(espn_live.DraftInit()) == {}

    def test_filled_roster_agrees_with_the_picks_made(self, init):
        picks = picks_from_init(init)
        for team in init.league.draft_teams:
            rostered = {item.player_id for item in team.draft_roster_items if item.player_id > 0}
            drafted = {row["player_id"] for row in picks if row["team_id"] == team.team_id}
            assert rostered == drafted


class TestSecurityToken:
    def test_builds_the_socket_parameter_from_the_response_body(self, monkeypatch):
        seen = {}

        class FakeResponse:
            text = "  abc123\n"

            def raise_for_status(self):
                seen["raised"] = True

        def fake_get(url, **kwargs):
            seen["url"] = url
            seen["kwargs"] = kwargs
            return FakeResponse()

        monkeypatch.setattr(espn_live.requests, "get", fake_get)
        token = espn_live.draft_security_token("1734659820", 2026, 3, "{SW-ID}", "s2")

        assert token == "1:1734659820:3:{SW-ID}:abc123"
        assert seen["raised"] is True
        assert seen["url"].endswith(
            "/apis/v3/games/ffl/seasons/2026/segments/0/leagues/1734659820/teams/3/draftSecurity"
        )
        assert seen["kwargs"]["cookies"] == {"SWID": "{SW-ID}", "espn_s2": "s2"}
        assert seen["kwargs"]["headers"]["X-Fantasy-Source"] == "kona"
        assert seen["kwargs"]["timeout"] == 20


class FakeSocket:
    """Stands in for a websockets sync connection. No network."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []

    def recv(self, timeout=None):
        if not self.frames:
            raise TimeoutError
        return self.frames.pop(0)

    def send(self, data):
        self.sent.append(data)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def socket_spy(monkeypatch):
    """Patch the connect() that fetch_init imports at call time, plus the token GET."""
    import websockets.sync.client

    seen = {}

    def install(frames):
        sock = FakeSocket(frames)
        seen["socket"] = sock

        def fake_connect(uri, **kwargs):
            seen["uri"] = uri
            seen["kwargs"] = kwargs
            return sock

        monkeypatch.setattr(websockets.sync.client, "connect", fake_connect)
        return seen

    monkeypatch.setattr(espn_live, "draft_security_token", lambda *a, **k: "TOKEN")
    return install


class TestFetchInit:
    def test_reads_the_init_frame_and_leaves(self, socket_spy, payload):
        seen = socket_spy([f"INIT {payload}\nSELECTED 3 4362628 4\n"])
        init, extra = espn_live.fetch_init("1734659820", 2026, 3, "{SW-ID}", "s2")

        assert init.league_id == 1734659820
        assert extra == ["SELECTED 3 4362628 4"]
        assert seen["socket"].sent == ["LEAVE\n"]

    def test_builds_the_join_url_with_an_unescaped_swid(self, socket_spy, payload):
        seen = socket_spy([f"INIT {payload}\n"])
        espn_live.fetch_init("1734659820", 2026, 3, "{SW-ID}", "s2")

        uri, query = seen["uri"].split("?")
        assert uri == "wss://fantasydraft.espn.com/game-1/league-1734659820/JOIN"
        params = dict(pair.split("=", 1) for pair in query.split("&"))
        assert params["1"] == "1"
        assert params["2"] == "1734659820"
        assert params["3"] == "3"
        assert params["4"] == "{SW-ID}"
        assert params["5"] == "TOKEN"
        assert params["6"] == "false"
        assert params["7"] == "false"
        assert params["8"] == "KONA"
        assert params["nocache"].isdigit()

    def test_sends_the_cookie_and_origin_headers(self, socket_spy, payload):
        seen = socket_spy([f"INIT {payload}\n"])
        espn_live.fetch_init("1734659820", 2026, 3, "{SW-ID}", "s2", timeout=9.0)

        kwargs = seen["kwargs"]
        assert kwargs["additional_headers"]["Cookie"] == "SWID={SW-ID}; espn_s2=s2"
        assert kwargs["additional_headers"]["Origin"] == "https://fantasy.espn.com"
        assert kwargs["user_agent_header"] == "Mozilla/5.0"
        assert kwargs["open_timeout"] == 9.0

    def test_collects_lines_that_arrive_before_the_init(self, socket_spy, payload):
        socket_spy(["TOKEN abc\n", f"INIT {payload}\n"])
        _, extra = espn_live.fetch_init("1734659820", 2026, 3, "{SW-ID}", "s2")
        assert extra == ["TOKEN abc"]

    def test_a_rejected_join_raises(self, socket_spy):
        socket_spy(["ERROR not authorized\n"])
        with pytest.raises(RuntimeError) as exc:
            espn_live.fetch_init("1734659820", 2026, 3, "{SW-ID}", "s2")
        assert "not authorized" in str(exc.value)

    def test_a_silent_socket_raises_rather_than_hanging(self, socket_spy):
        socket_spy([])
        with pytest.raises(RuntimeError) as exc:
            espn_live.fetch_init("1734659820", 2026, 3, "{SW-ID}", "s2", timeout=0.01)
        assert "INIT" in str(exc.value)


class TestFrameSplitting:
    def test_splits_on_newlines_and_drops_blanks(self):
        assert espn_live._lines("INIT abc\nSELECTED 3 1 2\n\n") == ["INIT abc", "SELECTED 3 1 2"]

    def test_accepts_binary_frames(self):
        assert espn_live._lines(b"CLOCK 3\n") == ["CLOCK 3"]
