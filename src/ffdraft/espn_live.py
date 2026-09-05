"""ESPN live-draft INIT decoder and a short-lived websocket client.

ESPN's kona draft client joins `wss://fantasydraft.espn.com` and receives a
newline-terminated text protocol. The first frame is `INIT <base64>`: a
hand-rolled big-endian binary snapshot of the whole draft room, written by a
chain of "storable transcoders" in draft.js. This module ports those decoders
byte for byte, so one connect/read/leave round trip yields every pick made so
far.

The transport is only used by `fetch_init`; `decode_init` is pure and offline.
"""
from __future__ import annotations

import base64
import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TypeVar

import requests

READS_HOST = "https://lm-api-reads.fantasy.espn.com"
DRAFT_HOST = "wss://fantasydraft.espn.com"

# ESPN's fantasyGameId for football.
FANTASY_GAME_ID = 1

_SIGN_BOUND = 2**31
_WRAP = 2**32


class Reader:
    """Byte reader matching class `He` in ESPN's draft.js.

    The JS builds a Uint16Array from `atob(payload).charCodeAt(i)`, so every
    element is a plain byte. Reading past the end raises here, where the JS
    would silently accumulate NaN.
    """

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.index = 0

    def _take(self, count: int) -> bytes:
        end = self.index + count
        if end > len(self.data):
            raise ValueError(
                f"read of {count} bytes at offset {self.index} runs past the "
                f"{len(self.data)}-byte payload"
            )
        chunk = self.data[self.index : end]
        self.index = end
        return chunk

    def read_number(self, count: int) -> int:
        return int.from_bytes(self._take(count), "big")

    def read_int(self) -> int:
        value = self.read_number(4)
        # Strictly greater, exactly as the JS: 0x80000000 stays positive.
        return value - _WRAP if value > _SIGN_BOUND else value

    def read_short(self) -> int:
        return self.read_number(2)

    def read_long(self) -> int:
        return self.read_number(8)

    def read_boolean(self) -> bool:
        return self.read_number(1) == 1

    def read_utf(self) -> str:
        return self._take(self.read_short()).decode("latin-1")

    def read_double(self) -> None:
        """Consume 8 bytes. The JS discards the value and returns a random number."""
        self._take(8)
        return None

    def read_float(self) -> None:
        """Consume 4 bytes. The JS discards the value and returns a random number."""
        self._take(4)
        return None

    def read_date(self) -> int | None:
        """Optional date: a non-zero marker int is followed by epoch millis."""
        if self.read_int() != 0:
            return self.read_long()
        return None

    def remaining(self) -> int:
        return len(self.data) - self.index


@dataclass
class DraftBlock:
    league_id: int = 0
    state: int = 0
    expiration_time: int | None = None
    nomination_team_index: int = 0
    up_for_bid_player_id: int = 0
    high_bid_team_id: int = 0
    high_bid_slot_id: int = 0
    high_bid_amount: int = 0


@dataclass
class DraftPick:
    league_id: int = 0
    team_id: int = 0
    pick_number: int = 0
    player_id: int = 0
    slot_id: int = 0
    bid_amount: int = 0
    nominating_team_id: int = 0
    is_keeper: bool = False
    autodraft_type_id: int = 0
    selector_user_profile_id: int = 0


@dataclass
class DraftPosition:
    league_id: int = 0
    position_id: int = 0
    position_max: int = 0


@dataclass
class BreakSchedule:
    league_id: int = 0
    interval: int = 0
    interval_type: int = 0


@dataclass
class AutodraftProtection:
    league_id: int = 0
    cutoff: int = 0
    cutoff_type: int = 0


@dataclass
class ScoringCategory:
    league_id: int = 0
    stat_id: int = 0
    # The JS discards the transcoded double, so the value is unrecoverable.
    scoring_value: None = None
    is_team_stat: bool = False


@dataclass
class ScoringSettings:
    league_id: int = 0
    scoring_type: int = 0
    scoring_categories: list[ScoringCategory | None] = field(default_factory=list)


@dataclass
class DraftRules:
    league_id: int = 0
    initial_pick_time: int = 0
    minimum_pick_time: int = 0
    nomination_time: int = 0
    selection_time: int = 0
    break_schedule: BreakSchedule | None = None
    autodraft_protection: AutodraftProtection | None = None
    pause_time: int = 0
    nomination_delay: int = 0
    minimum_bid: int = 0
    maximum_bid: int = 0
    # The four multipliers are transcoded doubles, discarded by the JS reader.
    minimum_high_bid_multiplier: None = None
    maximum_high_bid_multiplier: None = None
    minimum_current_bid_multiplier: None = None
    maximum_current_bid_multiplier: None = None
    default_balance: int = 0
    is_roster_completion_required: bool = False
    bench_slot_category_id: int = 0
    injury_slot_category_id: int = 0
    invalid_slot_category_id: int = 0
    is_censor_default: bool = False
    is_chat_capture_wanted: bool = False
    scoring_settings: ScoringSettings | None = None
    is_first_tier_protected: bool = False


@dataclass
class DraftSlotPosition:
    league_id: int = 0
    slot_category_id: int = 0
    position_id: int = 0


@dataclass
class DraftSlot:
    league_id: int = 0
    slot_id: int = 0
    slot_category_id: int = 0
    positions: list[DraftSlotPosition | None] = field(default_factory=list)


@dataclass
class DraftOwner:
    league_id: int = 0
    team_id: int = 0
    user_profile_id: int = 0
    is_lm: bool = False
    is_online: bool = False
    is_censor_enabled: bool = False


@dataclass
class DraftRosterItem:
    league_id: int = 0
    team_id: int = 0
    slot_id: int = 0
    player_id: int = 0
    is_keeper: bool = False


@dataclass
class DraftTeam:
    league_id: int = 0
    team_id: int = 0
    draft_position: int = 0
    autodraft_type_id: int = 0
    amount_left: int = 0
    owners: list[DraftOwner | None] = field(default_factory=list)
    draft_roster_items: list[DraftRosterItem | None] = field(default_factory=list)


@dataclass
class DraftLeague:
    league_id: int = 0
    draft_type: int = 0
    universe_id: int = 0
    draft_date: int | None = None
    draft_state: int = 0
    draft_block: DraftBlock | None = None
    draft_rules: DraftRules | None = None
    draft_positions: list[DraftPosition | None] = field(default_factory=list)
    draft_slots: list[DraftSlot | None] = field(default_factory=list)
    draft_picks: list[DraftPick | None] = field(default_factory=list)
    draft_teams: list[DraftTeam | None] = field(default_factory=list)


@dataclass
class DraftListPlayer:
    league_id: int = 0
    team_id: int = 0
    player_id: int = 0
    draft_value: int = 0
    ordinal_rank: int = 0


@dataclass
class DraftList:
    league_id: int = 0
    team_id: int = 0
    is_custom: bool = False
    draft_list_players: list[DraftListPlayer | None] = field(default_factory=list)


@dataclass
class NominationListPlayer:
    league_id: int = 0
    team_id: int = 0
    nomination_id: int = 0
    player_id: int = 0
    initial_bid: int = 0


@dataclass
class NominationList:
    league_id: int = 0
    team_id: int = 0
    nomination_list_players: list[NominationListPlayer | None] = field(default_factory=list)


@dataclass
class DraftInit:
    league_id: int = 0
    team_id: int = 0
    league: DraftLeague | None = None
    draft_list: DraftList | None = None
    nomination_list: NominationList | None = None


T = TypeVar("T")

# Presence marker ahead of every transcoded object. Anything else means the
# object was null and no further bytes belong to it.
_PRESENT = 1


def _header(reader: Reader, version: int, name: str) -> bool:
    """Consume the presence marker and version int. False means a null object."""
    if reader.read_int() != _PRESENT:
        return False
    found = reader.read_int()
    if found != version:
        raise ValueError(f"Version {found} not supported by {name} version {version}.")
    return True


def _array(reader: Reader, decoder: Callable[[Reader], T | None]) -> list[T | None]:
    count = reader.read_int()
    if count <= 0:
        return []
    return [decoder(reader) for _ in range(count)]


def _decode_draft_block(reader: Reader) -> DraftBlock | None:
    if not _header(reader, 1, "DraftBlockStorableTranscoder"):
        return None
    obj = DraftBlock()
    obj.league_id = reader.read_int()
    obj.state = reader.read_int()
    obj.expiration_time = reader.read_date()
    obj.nomination_team_index = reader.read_int()
    obj.up_for_bid_player_id = reader.read_int()
    obj.high_bid_team_id = reader.read_int()
    obj.high_bid_slot_id = reader.read_int()
    obj.high_bid_amount = reader.read_int()
    return obj


def _decode_draft_pick(reader: Reader) -> DraftPick | None:
    if not _header(reader, 3, "DraftPickStorableTranscoder"):
        return None
    obj = DraftPick()
    obj.league_id = reader.read_int()
    obj.team_id = reader.read_int()
    obj.pick_number = reader.read_int()
    obj.player_id = reader.read_int()
    obj.slot_id = reader.read_int()
    obj.bid_amount = reader.read_int()
    obj.nominating_team_id = reader.read_int()
    obj.is_keeper = reader.read_boolean()
    obj.autodraft_type_id = reader.read_int()
    obj.selector_user_profile_id = reader.read_int()
    return obj


def _decode_draft_position(reader: Reader) -> DraftPosition | None:
    if not _header(reader, 1, "DraftPositionStorableTranscoder"):
        return None
    obj = DraftPosition()
    obj.league_id = reader.read_int()
    obj.position_id = reader.read_int()
    obj.position_max = reader.read_int()
    return obj


def _decode_break_schedule(reader: Reader) -> BreakSchedule | None:
    if not _header(reader, 1, "BreakScheduleStorableTranscoder"):
        return None
    obj = BreakSchedule()
    obj.league_id = reader.read_int()
    obj.interval = reader.read_int()
    obj.interval_type = reader.read_int()
    return obj


def _decode_autodraft_protection(reader: Reader) -> AutodraftProtection | None:
    if not _header(reader, 1, "AutodraftProtectionStorableTranscoder"):
        return None
    obj = AutodraftProtection()
    obj.league_id = reader.read_int()
    obj.cutoff = reader.read_int()
    obj.cutoff_type = reader.read_int()
    return obj


def _decode_scoring_category(reader: Reader) -> ScoringCategory | None:
    if not _header(reader, 3, "ScoringCategoryStorableTranscoder"):
        return None
    obj = ScoringCategory()
    obj.league_id = reader.read_int()
    obj.stat_id = reader.read_int()
    obj.scoring_value = reader.read_double()
    obj.is_team_stat = reader.read_boolean()
    return obj


def _decode_scoring_settings(reader: Reader) -> ScoringSettings | None:
    if not _header(reader, 1, "ScoringSettingsStorableTranscoder"):
        return None
    obj = ScoringSettings()
    obj.league_id = reader.read_int()
    obj.scoring_type = reader.read_int()
    obj.scoring_categories = _array(reader, _decode_scoring_category)
    return obj


def _decode_draft_rules(reader: Reader) -> DraftRules | None:
    if not _header(reader, 2, "DraftRulesStorableTranscoder"):
        return None
    obj = DraftRules()
    obj.league_id = reader.read_int()
    obj.initial_pick_time = reader.read_int()
    obj.minimum_pick_time = reader.read_int()
    obj.nomination_time = reader.read_int()
    obj.selection_time = reader.read_int()
    obj.break_schedule = _decode_break_schedule(reader)
    obj.autodraft_protection = _decode_autodraft_protection(reader)
    obj.pause_time = reader.read_int()
    obj.nomination_delay = reader.read_int()
    obj.minimum_bid = reader.read_int()
    obj.maximum_bid = reader.read_int()
    obj.minimum_high_bid_multiplier = reader.read_double()
    obj.maximum_high_bid_multiplier = reader.read_double()
    obj.minimum_current_bid_multiplier = reader.read_double()
    obj.maximum_current_bid_multiplier = reader.read_double()
    obj.default_balance = reader.read_int()
    obj.is_roster_completion_required = reader.read_boolean()
    obj.bench_slot_category_id = reader.read_int()
    obj.injury_slot_category_id = reader.read_int()
    obj.invalid_slot_category_id = reader.read_int()
    obj.is_censor_default = reader.read_boolean()
    obj.is_chat_capture_wanted = reader.read_boolean()
    obj.scoring_settings = _decode_scoring_settings(reader)
    obj.is_first_tier_protected = reader.read_boolean()
    return obj


def _decode_draft_slot_position(reader: Reader) -> DraftSlotPosition | None:
    if not _header(reader, 1, "DraftSlotPositionStorableTranscoder"):
        return None
    obj = DraftSlotPosition()
    obj.league_id = reader.read_int()
    obj.slot_category_id = reader.read_int()
    obj.position_id = reader.read_int()
    return obj


def _decode_draft_slot(reader: Reader) -> DraftSlot | None:
    if not _header(reader, 1, "DraftSlotStorableTranscoder"):
        return None
    obj = DraftSlot()
    obj.league_id = reader.read_int()
    obj.slot_id = reader.read_int()
    obj.slot_category_id = reader.read_int()
    obj.positions = _array(reader, _decode_draft_slot_position)
    return obj


def _decode_draft_owner(reader: Reader) -> DraftOwner | None:
    if not _header(reader, 1, "DraftOwnerStorableTranscoder"):
        return None
    obj = DraftOwner()
    obj.league_id = reader.read_int()
    obj.team_id = reader.read_int()
    obj.user_profile_id = reader.read_int()
    obj.is_lm = reader.read_boolean()
    obj.is_online = reader.read_boolean()
    obj.is_censor_enabled = reader.read_boolean()
    return obj


def _decode_draft_roster_item(reader: Reader) -> DraftRosterItem | None:
    if not _header(reader, 1, "DraftRosterItemStorableTranscoder"):
        return None
    obj = DraftRosterItem()
    obj.league_id = reader.read_int()
    obj.team_id = reader.read_int()
    obj.slot_id = reader.read_int()
    obj.player_id = reader.read_int()
    obj.is_keeper = reader.read_boolean()
    return obj


def _decode_draft_team(reader: Reader) -> DraftTeam | None:
    if not _header(reader, 2, "DraftTeamStorableTranscoder"):
        return None
    obj = DraftTeam()
    obj.league_id = reader.read_int()
    obj.team_id = reader.read_int()
    obj.draft_position = reader.read_int()
    obj.autodraft_type_id = reader.read_int()
    obj.amount_left = reader.read_int()
    obj.owners = _array(reader, _decode_draft_owner)
    obj.draft_roster_items = _array(reader, _decode_draft_roster_item)
    return obj


def _decode_draft_league(reader: Reader) -> DraftLeague | None:
    if not _header(reader, 1, "DraftLeagueStorableTranscoder"):
        return None
    obj = DraftLeague()
    obj.league_id = reader.read_int()
    obj.draft_type = reader.read_int()
    obj.universe_id = reader.read_int()
    obj.draft_date = reader.read_date()
    obj.draft_state = reader.read_int()
    obj.draft_block = _decode_draft_block(reader)
    obj.draft_rules = _decode_draft_rules(reader)
    obj.draft_positions = _array(reader, _decode_draft_position)
    obj.draft_slots = _array(reader, _decode_draft_slot)
    obj.draft_picks = _array(reader, _decode_draft_pick)
    obj.draft_teams = _array(reader, _decode_draft_team)
    return obj


def _decode_draft_list_player(reader: Reader) -> DraftListPlayer | None:
    if not _header(reader, 1, "DraftListPlayerStorableTranscoder"):
        return None
    obj = DraftListPlayer()
    obj.league_id = reader.read_int()
    obj.team_id = reader.read_int()
    obj.player_id = reader.read_int()
    obj.draft_value = reader.read_int()
    obj.ordinal_rank = reader.read_int()
    return obj


def _decode_draft_list(reader: Reader) -> DraftList | None:
    if not _header(reader, 1, "DraftListStorableTranscoder"):
        return None
    obj = DraftList()
    obj.league_id = reader.read_int()
    obj.team_id = reader.read_int()
    obj.is_custom = reader.read_boolean()
    obj.draft_list_players = _array(reader, _decode_draft_list_player)
    return obj


def _decode_nomination_list_player(reader: Reader) -> NominationListPlayer | None:
    if not _header(reader, 1, "DraftNominationListPlayerStorableTranscoder"):
        return None
    obj = NominationListPlayer()
    obj.league_id = reader.read_int()
    obj.team_id = reader.read_int()
    obj.nomination_id = reader.read_int()
    obj.player_id = reader.read_int()
    obj.initial_bid = reader.read_int()
    return obj


def _decode_nomination_list(reader: Reader) -> NominationList | None:
    if not _header(reader, 1, "DraftNominationListStorableTranscoder"):
        return None
    obj = NominationList()
    obj.league_id = reader.read_int()
    obj.team_id = reader.read_int()
    obj.nomination_list_players = _array(reader, _decode_nomination_list_player)
    return obj


def _decode_draft_init(reader: Reader) -> DraftInit | None:
    if not _header(reader, 1, "DraftInitStorableTranscoder"):
        return None
    obj = DraftInit()
    obj.league_id = reader.read_int()
    obj.team_id = reader.read_int()
    obj.league = _decode_draft_league(reader)
    obj.draft_list = _decode_draft_list(reader)
    obj.nomination_list = _decode_nomination_list(reader)
    return obj


def decode_init(b64: str) -> DraftInit:
    """Decode an `INIT` payload into the full draft-room snapshot.

    Pure and offline. Raises ValueError on a null root or an unsupported version.
    """
    reader = Reader(base64.b64decode(b64))
    init = _decode_draft_init(reader)
    if init is None:
        raise ValueError("INIT payload carries a null DraftInit object")
    return init


def picks_from_init(init: DraftInit) -> list[dict]:
    """Picks already made, sorted by overall pick number.

    Unfilled future picks carry player_id -1 in this snapshot. 0 and a null pick
    object are rejected too, since neither names a player.
    """
    if init.league is None:
        return []
    made = []
    for pick in init.league.draft_picks:
        if pick is None or pick.player_id in (None, -1, 0):
            continue
        made.append(
            {
                "overall": pick.pick_number,
                "team_id": pick.team_id,
                "player_id": pick.player_id,
                "slot_id": pick.slot_id,
                "keeper": bool(pick.is_keeper),
            }
        )
    made.sort(key=lambda row: row["overall"])
    return made


def _signed(field: str) -> int | None:
    """The field as an int, or None when it is not one. ESPN encodes a team
    defense as a negative player id, so a leading minus is data, not a reject."""
    return int(field) if field.lstrip("-").isdigit() else None


def pick_event(line: str) -> dict | None:
    """What one wire line does to the pick list, or None if it does nothing.

    Only `SELECTED <teamId> <playerId> <lineupSlotId> <ownerSwid>` and
    `UNDONE <keepThrough>` change which picks exist; CLOCK, PONG, JOINED, LEFT,
    CHAT, SELECTING, DRAFT_LIST and the rest leave it alone and return None.

    A SELECTED or UNDONE whose fields are not numeric comes back as
    `{"event": "unparsed"}` rather than None. It announced a pick change nobody
    can apply, which a caller has to be able to count; dropping it silently is
    how a log ends up disagreeing with the state built from it.
    """
    fields = line.split(" ")
    kind = fields[0]
    if kind not in ("SELECTED", "UNDONE"):
        return None
    if kind == "SELECTED" and len(fields) >= 3:
        team_id, player_id = _signed(fields[1]), _signed(fields[2])
        if team_id is not None and player_id is not None:
            return {"event": "selected", "team_id": team_id, "player_id": player_id,
                    "slot_id": _signed(fields[3]) if len(fields) > 3 else None}
    if kind == "UNDONE" and len(fields) >= 2:
        keep = _signed(fields[1])
        if keep is not None:
            return {"event": "undone", "keep": keep}
    return {"event": "unparsed", "line": line}


def replay_picks(init: DraftInit, lines: Iterable[str]) -> list[dict]:
    """The picks as they stand after `lines`, starting from the INIT snapshot.

    INIT is initial state by the protocol's own semantics: it is sent once on
    join and never resent, so every pick made afterwards exists only as a
    SELECTED line. Reading the snapshot alone reports the draft as it was at
    join, however long ago that was.

    The arithmetic is the running watch's. A SELECTED lands at `len(picks) + 1`,
    which is `DraftState.on_the_clock`, and UNDONE drops every pick above the
    number it names, so a re-picked slot refills at the same number.
    `test_watch.py` holds the two side by side against the same lines.

    Each row is a `picks_from_init` row plus `source`: "init" for a pick the
    snapshot already held, "selected" for one replayed from an event.
    """
    picks = [{**pick, "source": "init"} for pick in picks_from_init(init)]
    for line in lines:
        event = pick_event(line)
        if event is None or event["event"] == "unparsed":
            continue
        if event["event"] == "selected":
            picks.append({"overall": len(picks) + 1, "team_id": event["team_id"],
                          "player_id": event["player_id"], "slot_id": event["slot_id"],
                          "keeper": False, "source": "selected"})
        else:
            picks = [pick for pick in picks if pick["overall"] <= event["keep"]]
    return picks


def slot_by_team(init: DraftInit) -> dict[int, int]:
    """ESPN team id -> 1-based draft slot. `draft_position` is zero-based in the
    snapshot: the team that picks first carries 0."""
    if init.league is None:
        return {}
    return {t.team_id: t.draft_position + 1 for t in init.league.draft_teams if t is not None}


def draft_security_token(
    league_id: str,
    season: int,
    team_id: int,
    swid: str,
    espn_s2: str,
) -> str:
    """Fetch the per-team draft security token and build the socket's `5=` parameter."""
    url = (
        f"{READS_HOST}/apis/v3/games/ffl/seasons/{season}/segments/0"
        f"/leagues/{league_id}/teams/{team_id}/draftSecurity"
    )
    resp = requests.get(
        url,
        cookies={"SWID": swid, "espn_s2": espn_s2},
        headers={
            "Accept": "application/json",
            "X-Fantasy-Source": "kona",
            "User-Agent": "ffdraft-mcp/1.0",
        },
        timeout=20,
    )
    resp.raise_for_status()
    return f"{FANTASY_GAME_ID}:{league_id}:{team_id}:{swid}:{resp.text.strip()}"


def _lines(frame: str | bytes) -> list[str]:
    text = frame.decode("utf-8", "replace") if isinstance(frame, bytes) else frame
    return [line.strip() for line in text.split("\n") if line.strip()]


def fetch_init(
    league_id: str,
    season: int,
    team_id: int,
    swid: str,
    espn_s2: str,
    timeout: float = 15.0,
) -> tuple[DraftInit, list[str]]:
    """Join the draft socket, take the INIT snapshot, and leave.

    Returns the decoded snapshot plus every other line seen, e.g.
    `SELECTED <teamId> <playerId> <slotId>`. The SWID keeps its braces and goes
    into the query string verbatim; a percent-encoded one is rejected.
    """
    init_b64, others = fetch_init_b64(league_id, season, team_id, swid, espn_s2, timeout)
    return decode_init(init_b64), others


def fetch_init_b64(
    league_id: str,
    season: int,
    team_id: int,
    swid: str,
    espn_s2: str,
    timeout: float = 15.0,
) -> tuple[str, list[str]]:
    """fetch_init without the decode: the INIT payload as ESPN sent it."""
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    token = draft_security_token(league_id, season, team_id, swid, espn_s2)
    uri = (
        f"{DRAFT_HOST}/game-{FANTASY_GAME_ID}/league-{league_id}/JOIN"
        f"?1={FANTASY_GAME_ID}&2={league_id}&3={team_id}&4={swid}&5={token}"
        f"&6=false&7=false&8=KONA&nocache={random.randint(0, _SIGN_BOUND - 1)}"
    )
    headers = {
        "Cookie": f"SWID={swid}; espn_s2={espn_s2}",
        "Origin": "https://fantasy.espn.com",
    }

    init_b64: str | None = None
    others: list[str] = []
    with connect(
        uri,
        additional_headers=headers,
        user_agent_header="Mozilla/5.0",
        open_timeout=timeout,
    ) as ws:
        deadline = time.monotonic() + timeout
        while init_b64 is None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise RuntimeError("ESPN draft socket sent no INIT frame before the timeout")
            try:
                frame = ws.recv(timeout=left)
            except TimeoutError as exc:
                raise RuntimeError(
                    "ESPN draft socket sent no INIT frame before the timeout"
                ) from exc
            for line in _lines(frame):
                if line.startswith("ERROR"):
                    raise RuntimeError(f"ESPN draft socket refused the join: {line}")
                if init_b64 is None and line.startswith("INIT "):
                    init_b64 = line[len("INIT ") :]
                else:
                    others.append(line)

        drain_until = time.monotonic() + 1.0
        while True:
            left = drain_until - time.monotonic()
            if left <= 0:
                break
            try:
                frame = ws.recv(timeout=left)
            except (TimeoutError, ConnectionClosed):
                break
            others.extend(_lines(frame))

        try:
            ws.send("LEAVE\n")
        except ConnectionClosed:
            pass

    return init_b64, others
