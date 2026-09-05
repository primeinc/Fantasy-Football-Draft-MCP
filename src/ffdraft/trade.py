"""Score a proposed trade for both sides over the rest of the season.

The question a trade evaluator has to answer is not "who got more points" but
"is the difference bigger than this harness's own noise". So every estimate here
arrives beside the spread between disjoint seed blocks of the same
configuration, and the verdict refuses to name a winner when those blocks
disagree in sign. That is the same contract the paired-draft backtests report
under, shared through `adp.block_agreement` rather than reimplemented.

WHAT IS SIMULATED, and what deliberately is not. A player's projection on the
board is `adj_ppg * exp_games`: a per-played-game rate times the games his
injury risk expects him to be available for. A weekly simulation that used
`proj_points` and *also* drew availability would charge the injury twice, which
is the error `roles.start_probability` documents for its own inputs. So a week
is `adj_ppg` if he is available and 0 if he is not, and availability comes from
`roles.weekly_availability`, the same mapping the board's own `exp_games` feeds.

Availability is the only thing drawn at random. Week-to-week scoring variance is
real and is not modelled, because the board carries no distribution for it and
inventing one would move the block spread -- the number a reader is meant to
judge the estimate against -- on the strength of a guess.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import adp as adp_mod
from . import roles
from .board import UNPRICED, is_position, with_stand_ins
from .config import LeagueSettings
from .names import normalize as norm_name

# The weeks a lineup is actually set for. Shared with roles.py so the two
# cannot disagree about how long the season is.
FANTASY_WEEKS = roles.FANTASY_WEEKS
# Trials per block, and blocks. The block count is adp's, for the same reason:
# one mean is not a finding.
DEFAULT_TRIALS = 200
DEFAULT_BLOCKS = adp_mod.DEFAULT_BLOCKS


# What priced a player's per-game rate. Not decoration: a roster can mix them,
# and a reader comparing two rosters is entitled to know which rows were read off
# the board and which were derived from a season total.
BASIS_BOARD = "adj_ppg"
BASIS_DERIVED = "proj_points / exp_games"
BASIS_NONE = "none: no projection on the board"
# A roster player the board cannot price at all, filled in at the position's
# replacement level through `board.with_stand_ins` -- the same stand-in every
# other tool gets, so a bystander is worth the same here as he is in a lineup or
# a waiver drop. Reported per side rather than folded into the total, because a
# delta that rests partly on replacement-level guesses is a weaker number than
# one that does not, and only the reader can decide how much weaker.
BASIS_STAND_IN = "replacement level: the board has no row for him"


@dataclass(frozen=True)
class Player:
    """One roster slot as the simulation needs it, resolved from the board once."""

    name: str
    key: str
    position: str
    adj_ppg: float
    exp_games: float
    bye_week: float | None
    basis: str = BASIS_BOARD

    @property
    def weekly_availability(self) -> float:
        return roles.weekly_availability(self.exp_games)


def _finite(value, fallback: float) -> float:
    """A number, or the fallback when it is missing or NaN.

    NaN is truthy, so `float(x) or fallback` keeps the NaN and one unprojected
    player turns a whole roster's season into NaN. `lineup_value` documents the
    same trap; this is the same guard at a different door.
    """
    if value is None:
        return fallback
    try:
        out = float(value)
    except (TypeError, ValueError):
        return fallback
    return out if np.isfinite(out) else fallback


def _stand_in_row(board: pd.DataFrame, name: str, position: str) -> dict | None:
    """One replacement-level row for a player the board does not carry.

    Built by `board.with_stand_ins`, the same helper `DraftState.my_rows` and
    `rosters.roster_rows` use, so a player unknown to the board is worth exactly
    the same here as he is in a lineup or a waiver drop. A second pricing rule
    for the same situation is how `_discount` came to exist twice.
    """
    if not is_position(position) or "_key" not in board.columns:
        return None
    empty = board.iloc[0:0].copy()
    empty[UNPRICED] = pd.Series(dtype=bool)
    built = with_stand_ins(empty, board, [(name, position)])
    return None if built.empty else built.iloc[0].to_dict()


def resolve(board: pd.DataFrame, names: list[str],
            positions: Mapping[str, str] | None = None,
            ) -> tuple[list[Player], list[str]]:
    """Board rows for these names, plus the names that matched nothing.

    A name the board cannot price becomes a replacement-level stand-in when
    `positions` says what he plays, and is returned as missing only when nothing
    can place him. That is what lets a trade be scored at all: the live roster
    holds MarShawn Lloyd, whom the board has no row for, and refusing the whole
    evaluation over a player who is not in the trade told the user nothing about
    the trade they asked about.

    Unmatched names still come back rather than being dropped. The caller
    decides what a miss means, and for `evaluate` it means different things on
    the two sides: a name in `give` or `get` stops the evaluation, because a
    trade scored without one of its own pieces is a different trade, while a
    bystander is priced and reported.
    """
    if "_key" not in board.columns:
        return [], list(names)
    rows = {k: row for k, row in zip(board["_key"], board.to_dict("records"))}
    place = dict(positions or {})
    found, missing = [], []
    for name in names:
        row = rows.get(norm_name(name))
        if row is None:
            row = _stand_in_row(board, name, str(place.get(norm_name(name)) or ""))
            if row is None:
                missing.append(name)
                continue
            found.append(Player(
                name=name, key=norm_name(name),
                position=str(row.get("position") or ""),
                adj_ppg=_finite(row.get("proj_points"), 0.0) / roles.SEASON_GAMES,
                exp_games=float(roles.SEASON_GAMES), bye_week=None,
                basis=BASIS_STAND_IN))
            continue
        exp_games = _finite(row.get("exp_games"), roles.SEASON_GAMES)
        adj = row.get("adj_ppg")
        basis = BASIS_BOARD
        if adj is None or not np.isfinite(_finite(adj, np.nan)):
            # No per-game rate: fall back to the season projection spread over the
            # games it was built from, which is the identity `proj_points =
            # adj_ppg * exp_games` read backwards. A kicker or a defense lands
            # here, and so does any row the projection could not model.
            #
            # The fallback is reported per player rather than applied quietly. A
            # roster can mix bases, and which rows were derived is exactly what a
            # reader needs to weigh a delta built from them.
            proj = row.get("proj_points")
            have_proj = proj is not None and np.isfinite(_finite(proj, np.nan))
            # `exp_games > 0` is part of the condition, not a guard after it: with
            # no games to divide by, no division happens and the result is 0, so
            # labelling it derived would put a `BASIS_NONE` outcome under the
            # derived name. `model.project` clips exp_games to [7, 17] so the live
            # board cannot produce this, but `resolve` accepts any board and the
            # fixtures build them by hand.
            basis = BASIS_DERIVED if have_proj and exp_games > 0 else BASIS_NONE
            adj = _finite(proj, 0.0) / exp_games if exp_games > 0 else 0.0
        bye = row.get("bye_week")
        bye = int(bye) if bye is not None and np.isfinite(_finite(bye, np.nan)) else None
        found.append(Player(
            name=str(row.get("name") or name), key=norm_name(name),
            position=str(row.get("position") or ""),
            adj_ppg=_finite(adj, 0.0), exp_games=exp_games, bye_week=bye, basis=basis))
    return found, missing


def _available(seed: int, key: str, week: int) -> float:
    """A uniform in [0, 1) fixed by (seed, player, week) alone.

    Drawn from a hash rather than a sequential RNG on purpose. A sequential
    stream hands out draws in roster order, so adding or removing one player
    shifts every later draw and the before/after difference measures the
    reshuffle as much as the trade. Keyed this way, every player a trade does not
    touch has an identical season on both sides of it, and the delta is the
    trade.
    """
    digest = hashlib.blake2b(f"{seed}|{key}|{week}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / float(1 << 64)


def simulate_season(roster: list[Player], league: LeagueSettings, seed: int,
                    weeks: int = FANTASY_WEEKS) -> dict:
    """One roster's season: best legal lineup each week, summed.

    A player is out in a week when it is his bye or his availability draw fails;
    otherwise he scores his per-game rate. Kicker and defense slots are not
    scored, which is `adp.best_weekly_lineup`'s existing behaviour rather than a
    choice made here.

    THE BYE IS CHARGED ONCE, and the reason is not obvious enough to leave
    implicit. This both skips the bye week and applies an availability derived
    from a 17 denominator, which looks like charging it twice. It is not:
    `roles.SEASON_GAMES` is 17 *games*, and the NFL season is 18 weeks with one
    bye, so `exp_games / 17` is the chance he is available in a week he has a
    game at all. The bye is the eighteenth week, and the `continue` below is the
    only thing that prices it. Were the denominator weeks, this would be a double
    count. Measured: a player at `exp_games` 17 with a bye scores exactly 13
    weeks of his rate over a 14-week window.
    """
    positions = {p.key: p.position for p in roster}
    total, empty = 0.0, 0
    for week in range(1, weeks + 1):
        points = {}
        for p in roster:
            if p.bye_week == week:
                continue
            if _available(seed, p.key, week) < p.weekly_availability:
                points[p.key] = p.adj_ppg
        week_points, week_empty = adp_mod.best_weekly_lineup(
            points, positions, league.starters, league.flex_eligible)
        total += week_points
        empty += week_empty
    return {"points": round(total, 1), "empty_slots": empty}


def compare(before: list[Player], after: list[Player], league: LeagueSettings,
            n_trials: int = DEFAULT_TRIALS, blocks: int = DEFAULT_BLOCKS,
            seed: int = 0, weeks: int = FANTASY_WEEKS) -> dict:
    """Season points for one roster before and after, in disjoint seed blocks.

    Blocks use `seed + block * n_trials + trial`, so a second block extends the
    sample rather than repeating it -- the same arithmetic `adp._paired_blocks`
    uses, for the same reason.
    """
    rows = []
    for block in range(blocks):
        block_seed = seed + block * n_trials
        gains, before_pts, after_pts, empty_before, empty_after = [], [], [], [], []
        for trial in range(n_trials):
            trial_seed = block_seed + trial
            a = simulate_season(before, league, trial_seed, weeks)
            b = simulate_season(after, league, trial_seed, weeks)
            gains.append(b["points"] - a["points"])
            before_pts.append(a["points"])
            after_pts.append(b["points"])
            empty_before.append(a["empty_slots"])
            empty_after.append(b["empty_slots"])
        rows.append({
            "block": block, "seed_from": block_seed, "n_trials": n_trials,
            "points_before": round(float(np.mean(before_pts)), 1),
            "points_after": round(float(np.mean(after_pts)), 1),
            "improvement": round(float(np.mean(gains)), 1),
            "trials_improved": int(sum(1 for g in gains if g > 0)),
            "empty_slots_before": round(float(np.mean(empty_before)), 2),
            "empty_slots_after": round(float(np.mean(empty_after)), 2),
        })
    return {"blocks": rows,
            # A replication harness, declared as one. Nothing is fitted here:
            # the blocks re-run the same simulation on disjoint seeds, and its
            # inputs are already in points (`adj_ppg` per available week), so
            # there is no held-out set for the calibration rule's second clause
            # to be tested on. `adp.margin_unit` grants points on sign agreement
            # for that reason and only when the input unit is declared, which is
            # what makes the claim checkable rather than assumed.
            **adp_mod.block_agreement([r["improvement"] for r in rows],
                                      harness=adp_mod.HARNESS_REPLICATION,
                                      input_unit=adp_mod.UNIT_POINTS),
            "trials_improved": sum(r["trials_improved"] for r in rows),
            "trials": n_trials * blocks}


def depth(roster: list[Player], league: LeagueSettings) -> dict:
    """Players per position against the slots the league starts.

    `spare` is what is left after the starters at that position are filled, which
    is what a trade spends. Negative means the trade left a slot it cannot fill.
    """
    counts = Counter(p.position for p in roster if p.position)
    out = {}
    for pos in sorted(set(counts) | {p for p, n in league.starters.items() if n}):
        if pos in ("FLEX",):
            continue
        starts = league.starters.get(pos, 0)
        out[pos] = {"rostered": counts.get(pos, 0), "starts": starts,
                    "spare": counts.get(pos, 0) - starts}
    return out


def priced_by(roster: list[Player]) -> dict:
    """How many rows were read off the board, and which were not.

    A roster can mix bases. Naming the exceptions rather than only counting them
    is what lets a reader decide whether a delta rests on rows the projection
    actually modelled.
    """
    counts = Counter(p.basis for p in roster)
    return {"counts": dict(sorted(counts.items())),
            "not_from_the_board": [{"name": p.name, "position": p.position,
                                    "basis": p.basis, "per_game": round(p.adj_ppg, 2)}
                                   for p in roster if p.basis != BASIS_BOARD]}


def verdict(summary: dict, side: str, weeks: int = FANTASY_WEEKS) -> str:
    """What may be said about this side, given whether the blocks agree.

    A mean whose blocks disagree in sign is a measurement of the harness, not of
    the trade, and this refuses to call it either way. Agreement is not a pass
    either: at two blocks it is one coin flip, which `blocks_agree_p_null` states
    beside it.

    `weeks` is the window that was actually scored, passed in rather than read
    off the module constant. The sentence a human reads is the last place a
    window may be stated wrong, and it used to say 14 whatever the run did.

    The word "points" is likewise not this function's to choose. It comes from
    the verdict `adp.margin_unit` put in the summary, so the sentence and the
    structured `unit` field cannot disagree about what the number is.
    """
    gain = summary.get("improvement")
    spread = summary.get("block_spread")
    if gain is None:
        return f"{side}: nothing to compare"
    if not summary.get("blocks_agree"):
        return (f"{side}: no call. The blocks disagree in sign "
                f"({summary['block_improvements']}), so {gain:+.1f} points is inside "
                f"this harness's own noise rather than a result.")
    direction = "gains" if gain > 0 else "loses"
    p_null = summary.get("blocks_agree_p_null")
    # The unit is read, never asserted. Hardcoding the word here is exactly what
    # the rule exists to stop, and it is where a sentence and its own structured
    # field come to disagree.
    unit = summary.get("unit", adp_mod.UNIT_ORDINAL)
    amount = (f"{abs(gain):.1f} points" if unit == adp_mod.UNIT_POINTS
              else f"{abs(gain):.1f} ({unit})")
    # On a pass this is the rider that says what the spread does not cover; on
    # an ordinal answer it is the clause that failed. Either way the sentence
    # carries the reason rather than leaving it in a field nobody reads.
    tail = summary.get("spread_covers") if unit == adp_mod.UNIT_POINTS \
        else summary.get("unit_reason")
    tail = f" {str(tail)[:1].upper()}{str(tail)[1:]}." if tail else ""
    return (f"{side} {direction} {amount} over {weeks} weeks, "
            f"blocks {summary['block_improvements']}, spread {spread}. Blocks of a "
            f"trade worth nothing agree in sign with probability {p_null}, so read "
            f"the spread before the mean." + tail)


def _spread_note(roster: list[Player]) -> str | None:
    """What this side's `block_spread` does not include, when that is a stand-in.

    A stand-in is given full expected games, because the board has no injury
    opinion about a player it has no row for and inventing one would be a guess
    dressed as data. The consequence lands on the spread rather than on the
    mean: availability is the only thing drawn at random here, so a player who is
    always available contributes no variance, and `block_spread` comes out
    narrower than the roster warrants. Narrower is the direction that flatters a
    delta, so it is said beside the number rather than left for a reader to
    deduce from the basis column.
    """
    names = [p.name for p in roster if p.basis == BASIS_STAND_IN]
    if not names:
        return None
    return ("block_spread excludes the week-to-week variance of "
            + ", ".join(names)
            + ": priced at replacement level with full expected games, so their "
              "availability never varies and the spread is narrower than this "
              "roster warrants. They are in the estimate as well as absent from "
              "its spread -- freddy measured a pair of stand-ins moving an "
              "improvement from +36.9 to -3.9 while the spread tightened from "
              "4.3 to 0.7, so this side's verdict can turn on them and reads "
              "more confident as it does. Their points are under stand_ins.")


def _stand_ins_of(roster: list[Player], weeks: int) -> list[dict]:
    """The replacement-level fill-ins on a roster, with what each contributes.

    Points rather than a bare name, because "two of these are guesses" and "two
    of these are guesses worth 96 points between them" are different warnings,
    and only the second lets a reader judge whether the delta survives them.
    """
    return [{"player": p.name, "position": p.position,
             "points": round(p.adj_ppg * p.weekly_availability * weeks, 1),
             "basis": p.basis}
            for p in roster if p.basis == BASIS_STAND_IN]


def _roster_names(picks: list[dict]) -> list[str]:
    return [str(p["name"]) for p in picks]


def _swap(names: list[str], out: list[str], into: list[str]) -> list[str]:
    gone = {norm_name(n) for n in out}
    return [n for n in names if norm_name(n) not in gone] + list(into)


def evaluate(board: pd.DataFrame, picks_by_slot: dict[int, list[dict]],
             league: LeagueSettings, my_slot: int, counterparty_slot: int,
             give: list[str], get: list[str], n_trials: int = DEFAULT_TRIALS,
             blocks: int = DEFAULT_BLOCKS, seed: int = 0,
             weeks: int = FANTASY_WEEKS) -> dict:
    """Both sides of one proposed trade, before and after, with the spread.

    `give` leaves your roster and `get` arrives on it; the counterparty's roster
    moves the other way, so one simulation answers for both and the two sides
    cannot be scored under different assumptions.

    A player named on the wrong roster, or on no board row, stops the evaluation
    rather than being dropped. A trade scored without one of its own pieces is a
    different trade.
    """
    mine = _roster_names(picks_by_slot.get(my_slot, []))
    theirs = _roster_names(picks_by_slot.get(counterparty_slot, []))
    errors = []
    for name in give:
        if norm_name(name) not in {norm_name(n) for n in mine}:
            errors.append(f"{name!r} is not on your roster (slot {my_slot})")
    for name in get:
        if norm_name(name) not in {norm_name(n) for n in theirs}:
            errors.append(f"{name!r} is not on slot {counterparty_slot}'s roster")
    if not give and not get:
        errors.append("a trade needs at least one player on one side")

    rosters = {
        "mine_before": mine, "mine_after": _swap(mine, give, get),
        "theirs_before": theirs, "theirs_after": _swap(theirs, get, give),
    }
    # Positions from the draft record, which knows what a player is even when the
    # board cannot price him -- that is the whole reason `record_pick` files one.
    # Deliberately NOT offered for a name in the trade: a stand-in is a guess,
    # and the pieces being valued are the one thing this may not guess about. A
    # traded player with no board row still stops the evaluation, because a trade
    # scored on a replacement-level estimate of its own centrepiece is a
    # confident answer to a question the board cannot answer.
    #
    # A bystander is the opposite case. He is priced and reported, because
    # refusing the whole trade over a player who is not in it answers nothing the
    # user asked: the live roster holds MarShawn Lloyd, who has no board row, and
    # every trade on that roster was refused for him.
    traded = {norm_name(n) for n in list(give) + list(get)}
    positions = {norm_name(p["name"]): str(p.get("position") or "")
                 for picks in picks_by_slot.values() for p in picks
                 if norm_name(p["name"]) not in traded}
    resolved, missing = {}, []
    for label, names in rosters.items():
        players, gone = resolve(board, names, positions)
        resolved[label] = players
        missing.extend(gone)
    blocking = sorted({n for n in missing if norm_name(n) in traded})
    if blocking:
        # Says why THIS name refuses when a bystander does not. The bare "no
        # board row for" was read as a bug the last time it fired, correctly,
        # because it fired on a player who was not in the trade. Now that it
        # cannot, the message has to carry the distinction or the next reader
        # files the same report. Flagged by freddy.
        errors.append(
            "no board row for: " + ", ".join(blocking)
            + " -- a player being traded cannot be filled in at replacement "
              "level, because he is the quantity the answer is about. A player "
              "on either roster who is not in the trade is stood in for and "
              "reported under stand_ins.")
    unplaceable = sorted({n for n in missing if norm_name(n) not in traded})
    if errors:
        return {"ok": False, "errors": errors}

    yours = compare(resolved["mine_before"], resolved["mine_after"], league,
                    n_trials, blocks, seed, weeks)
    theirs_cmp = compare(resolved["theirs_before"], resolved["theirs_after"], league,
                         n_trials, blocks, seed, weeks)
    return {
        "ok": True,
        # Which weeks were scored, not just how many. Read from week 1, so this
        # is a season-long answer; a trade being weighed in week 9 is asking
        # about weeks 9 to 14 and this does not yet know the difference.
        "weeks": {"from": 1, "to": weeks},
        "give": list(give),
        "get": list(get),
        # Players on either roster the board could not price, filled in at their
        # position's replacement level with the points each contributes, so the
        # total is never silently moved by a guess. Empty means both rosters are
        # fully priced and the delta rests on the board throughout.
        "stand_ins": {
            "yours": _stand_ins_of(resolved["mine_after"], weeks),
            "theirs": _stand_ins_of(resolved["theirs_after"], weeks),
        },
        # Named on a roster but placeable by nothing -- no board row and no
        # recorded position. They are absent from the simulation entirely, which
        # is a smaller roster than the user has, so it is said rather than left
        # to be inferred from a total.
        "not_scored": unplaceable,
        "you": {
            "slot": my_slot, **yours,
            "depth_before": depth(resolved["mine_before"], league),
            "depth_after": depth(resolved["mine_after"], league),
            "priced_by": priced_by(resolved["mine_after"]),
            "spread_note": _spread_note(resolved["mine_after"]),
            "verdict": verdict(yours, "you", weeks),
        },
        "counterparty": {
            "slot": counterparty_slot, **theirs_cmp,
            "depth_before": depth(resolved["theirs_before"], league),
            "depth_after": depth(resolved["theirs_after"], league),
            "priced_by": priced_by(resolved["theirs_after"]),
            "spread_note": _spread_note(resolved["theirs_after"]),
            "verdict": verdict(theirs_cmp, f"slot {counterparty_slot}", weeks),
            "tendencies": counterparty_tendencies(
                picks_by_slot.get(counterparty_slot, []), board),
        },
        # Both sides can gain: they start different lineups, so the same player is
        # worth different points to each. Both sides losing is the tell that the
        # trade empties a starting slot somebody was filling.
        "reading": ("Each side is scored on its own lineup, so both can gain and both "
                    "can lose. Read block_spread before improvement, and treat a side "
                    "whose blocks disagree as unmeasured rather than even."),
    }


def counterparty_tendencies(picks: list[dict], board: pd.DataFrame) -> dict:
    """What the counterparty's own draft says about how they value positions.

    Their picks by position, and how far from ADP they took them. A positive
    `mean_adp_delta` means they let players fall to them; a negative one means
    they reached. This is the draft record, not a model of the person: it says
    what they did, and the reader decides what it is worth.
    """
    if not picks:
        return {"picks": 0, "by_position": {}, "mean_adp_delta": None,
                "note": "no draft record for this team"}
    adp_of: dict[str, float] = {}
    if "_key" in board.columns and "adp" in board.columns:
        adp_of = {k: v for k, v in zip(board["_key"], board["adp"])}
    deltas = []
    for pick in picks:
        value = adp_of.get(norm_name(pick["name"]))
        overall = pick.get("overall")
        if value is None or overall is None or not np.isfinite(_finite(value, np.nan)):
            continue
        deltas.append(float(value) - float(overall))
    by_position = Counter(str(p.get("position") or "unknown") for p in picks)
    return {
        "picks": len(picks),
        "by_position": dict(sorted(by_position.items())),
        "mean_adp_delta": round(float(np.mean(deltas)), 1) if deltas else None,
        "priced_picks": len(deltas),
        "reading": ("positive mean_adp_delta: they took players later than the market "
                    "did; negative: they reached ahead of it"),
    }
