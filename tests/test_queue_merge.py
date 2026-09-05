"""set_draft_queue keeps what the user queued.

The queue has two authors, the user in the ESPN app and this server, and ESPN's
protocol carries no add or remove -- `DRAFT_LIST` is the whole list. So a call
that sends only its own names silently deletes everything the user built. That
is what these tests are about.
"""
import asyncio
import json

import pandas as pd
import pytest

from ffdraft import board, server, watch
from ffdraft.config import LeagueSettings

# ESPN ids, and the names the fake crosswalk gives them.
USER_QUEUE = [4429795, 4362628, 3916433]
OURS = [4429205, 4429059]
NAMES = {"4429795": "Jahmyr Gibbs", "4362628": "Ja'Marr Chase", "3916433": "Cam Skattebo",
         "4429205": "Bijan Robinson", "4429059": "Puka Nacua"}
POSITIONS = {"4429795": "RB", "4362628": "WR", "3916433": "RB",
             "4429205": "RB", "4429059": "WR"}


class Live:
    """The running watch and everything the fake socket saw leave it."""

    def __init__(self, draft_watch, sent: list[str]) -> None:
        self.watch = draft_watch
        self.sent = sent

    @property
    def queue(self):
        return self.watch.queue

    @property
    def queue_echoes(self):
        return self.watch.queue_echoes

    def echo(self, line: str) -> None:
        """ESPN sending a line to us, rather than us sending one to ESPN."""
        asyncio.run(self.watch.handle_line(line))


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A watch whose socket echoes back whatever it is sent, wired into _WATCHES."""
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    monkeypatch.setattr(watch, "STATE_DIR", tmp_path)
    monkeypatch.setattr(board, "espn_maps", lambda: (dict(NAMES), dict(POSITIONS)))
    league = LeagueSettings(name="t", teams=12, draft_slot=4, rounds=14)

    async def notify(content, meta):
        return None

    w = watch.DraftWatch("L", 2026, 3, "{A}", "s2", league, None, notify)
    w.connected = True
    w.espn_map = dict(NAMES)
    sent: list[str] = []

    class Ws:
        async def send(self, text):
            sent.append(text)
            await w.handle_line(text.strip())

    monkeypatch.setattr(w, "ws", Ws())

    b = pd.DataFrame({"name": list(NAMES.values()),
                      "position": [POSITIONS[k] for k in NAMES],
                      "espn_id": [int(k) for k in NAMES]})
    b["_key"] = b["name"].map(board.norm_name)
    monkeypatch.setattr(server, "_build_board", lambda force=False: b)
    monkeypatch.setattr(board, "resolve_espn_id",
                        lambda name, board_df, espn_map: (
                            next((int(k) for k, v in NAMES.items()
                                  if board.norm_name(v) == board.norm_name(name)), None),
                            f"no match for {name!r}"))
    monkeypatch.setitem(server._WATCHES, "L", (w, None))
    return Live(w, sent)


def _call(**kw) -> dict:
    return json.loads(asyncio.run(server.set_draft_queue(league_id="L", **kw)))


def _ids(rows: list[dict]) -> list[int]:
    return [r["espn_id"] for r in rows]


def _seed_user_queue(live: Live) -> None:
    """The user built a queue in the ESPN app and ESPN echoed it."""
    live.echo("DRAFT_LIST " + " ".join(str(i) for i in USER_QUEUE))
    assert live.queue == USER_QUEUE


class TestMergeIsTheDefault:
    def test_our_names_go_first_and_the_users_players_are_all_kept(self, live):
        _seed_user_queue(live)

        out = _call(player_names="Bijan Robinson, Puka Nacua")

        assert out["mode"] == "merge"
        assert _ids(out["sent"]) == OURS + USER_QUEUE
        assert _ids(out["accepted"]) == OURS + USER_QUEUE
        assert out["removed"] == []
        # The report says which entries were already the user's, so a reader can
        # see the tool did not invent them.
        assert _ids(out["kept_from_the_users_queue"]) == USER_QUEUE

    def test_the_player_who_went_missing_survives_a_merge(self, live):
        """The report that opened this: a player was in the queue before pick 61
        and gone afterwards. Under a merge he is still there."""
        _seed_user_queue(live)

        out = _call(player_names="Bijan Robinson")

        names = [r["name"] for r in out["accepted"]]
        assert "Cam Skattebo" in names
        assert live.queue is not None and 3916433 in live.queue

    def test_a_player_we_send_that_is_already_queued_is_not_duplicated(self, live):
        _seed_user_queue(live)

        out = _call(player_names="Ja'Marr Chase, Bijan Robinson")

        sent = _ids(out["sent"])
        assert sent == [4362628, 4429205, 4429795, 3916433]
        assert len(sent) == len(set(sent))


class TestReplaceIsExplicitAndNamesWhatItRemoved:
    def test_replace_sends_only_ours(self, live):
        _seed_user_queue(live)

        out = _call(player_names="Bijan Robinson, Puka Nacua", replace=True)

        assert out["mode"] == "replace"
        assert _ids(out["sent"]) == OURS
        assert _ids(out["accepted"]) == OURS

    def test_every_removed_player_is_named(self, live):
        _seed_user_queue(live)

        out = _call(player_names="Ja'Marr Chase", replace=True)

        # Chase was on both lists, so he is not a removal; the other two are.
        assert sorted(r["name"] for r in out["removed"]) == ["Cam Skattebo", "Jahmyr Gibbs"]
        assert _ids(out["queue_before"]) == USER_QUEUE


class TestAnUnknownQueueIsNotOverwritten:
    def test_a_merge_is_refused_when_espn_has_echoed_nothing(self, live):
        assert live.queue is None

        out = _call(player_names="Bijan Robinson")

        assert "error" in out
        assert live.sent == [], "nothing may go over the socket"
        assert _ids(out["would_send"]) == [4429205]
        assert "replace=True" in out["do"]

    def test_replace_still_works_without_an_echo_because_it_was_asked_for(self, live):
        assert live.queue is None

        out = _call(player_names="Bijan Robinson", replace=True)

        assert out["mode"] == "replace"
        assert _ids(out["sent"]) == [4429205]
        # Nothing is claimed to have been removed, because nothing was known.
        assert out["removed"] == []


class TestTheEchoHistory:
    def test_every_echo_is_kept_with_a_timestamp(self, live):
        _seed_user_queue(live)
        live.echo("DRAFT_LIST 4429795 4362628")

        assert [ids for _ts, ids in live.queue_echoes] == [USER_QUEUE, [4429795, 4362628]]
        assert all(ts > 0 for ts, _ids in live.queue_echoes)

    def test_draft_queue_reports_the_history_so_a_loss_has_a_when(self, live):
        _seed_user_queue(live)
        live.echo("DRAFT_LIST 4429795 4362628")

        out = json.loads(asyncio.run(server.draft_queue(league_id="L")))

        assert len(out["echoes"]) == 2
        gone = set(_ids(out["echoes"][0]["queue"])) - set(_ids(out["echoes"][1]["queue"]))
        assert gone == {3916433}

    def test_an_id_espn_sends_that_cannot_be_read_does_not_kill_the_session(self, live):
        # `--5` passes a lstrip-then-isdigit test and then raises in int(). The
        # same split cost two fixes elsewhere in this package; the queue reader
        # lets int() decide.
        live.echo("DRAFT_LIST 4429795 --5 4362628")
        assert live.queue == [4429795, 4362628]
