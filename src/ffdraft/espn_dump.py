"""Everything ESPN will say about one league's draft, written to disk as-is.

Two surfaces. The read API (`lm-api-reads`) answers one JSON document per
`view`; each is saved untouched under `read_api/`. The draft-room socket's INIT
snapshot is saved raw (base64) and decoded under `live/`, with every line the
watch has seen since it joined, timestamped, so pick timing exists somewhere.

`live/` holds two pick lists on purpose. INIT is initial state: it is sent once
on join and never resent, so `init.json` and `picks.json` describe the draft as
it stood at join and stay that way, as evidence. `state.json` is the draft now,
INIT replayed through the SELECTED and UNDONE lines since. Every `live` entry in
the manifest carries the pick count it is as-of, so the two are never read as
the same number. `reconcile.json` compares the current state against the read
API's own `mDraftDetail`.
"""
from __future__ import annotations

import dataclasses
import json
import os
import time
from pathlib import Path

import requests

from . import espn_live
from .config import CURRENT_SEASON

READS_HOST = "https://lm-api-reads.fantasy.espn.com"

# Every league view the kona client requests somewhere in its lifecycle.
READ_VIEWS = (
    "mSettings", "mTeam", "mRoster", "mDraftDetail", "mMatchup", "mMatchupScore",
    "mSchedule", "mScoreboard", "mStatus", "mNav", "mPendingTransactions",
    "mLiveScoring", "mBoxscore", "mPositionalRatings", "kona_league_communication",
)
PLAYER_FILTER = {"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]},
                             "limit": 2000,
                             "sortDraftRanks": {"sortPriority": 100, "sortAsc": True,
                                                "value": "PPR"}}}


def _cookies(swid: str, espn_s2: str) -> dict[str, str]:
    from .board import espn_cookies

    return espn_cookies(swid, espn_s2)


def _get(url: str, params: dict, cookies: dict[str, str],
         extra_headers: dict[str, str] | None = None) -> requests.Response:
    headers = {"User-Agent": "ffdraft-mcp/1.0", "X-Fantasy-Source": "kona",
               "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    return requests.get(url, params=params, cookies=cookies, headers=headers, timeout=30)


def _write(path: Path, resp: requests.Response) -> dict:
    """Save the body exactly as received; JSON is re-indented only when it parses."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = resp.content
    try:
        parsed = resp.json()
        path.write_text(json.dumps(parsed, indent=1), encoding="utf-8")
    except ValueError:
        path.write_bytes(body)
    return {"file": path.name, "status": resp.status_code, "bytes": len(body),
            "url": resp.url}


def dump_draft(league_id: str, out_dir: str | os.PathLike, season: int = CURRENT_SEASON,
               swid: str | None = None, espn_s2: str | None = None,
               init_b64: str | None = None, lines: list[tuple[int, str]] | None = None,
               team_id: int | None = None) -> dict:
    """Write the dump and return its manifest (also saved as manifest.json).

    `init_b64` and `lines` come from a running watch. Without them, `team_id`
    opens the draft socket once to take a snapshot, which bumps any other
    connection for that team (the browser room or a watch).
    """
    swid = swid or os.environ.get("ESPN_SWID") or ""
    espn_s2 = espn_s2 or os.environ.get("ESPN_S2") or ""
    if not (swid and espn_s2):
        raise RuntimeError("ESPN_SWID and ESPN_S2 are required")
    cookies = _cookies(swid, espn_s2)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = Path(out_dir).resolve() / f"espn_dump_{league_id}_{season}_{stamp}"
    read_dir, live_dir = root / "read_api", root / "live"
    read_dir.mkdir(parents=True, exist_ok=True)
    live_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"league_id": league_id, "season": season, "taken_at_ms": int(time.time() * 1000),
                      "root": str(root), "read_api": [], "live": [], "errors": []}

    from .board import espn_league_url

    base = espn_league_url(league_id, season)
    for view in READ_VIEWS:
        entry = _write(read_dir / f"{view}.json", _get(base, {"view": view}, cookies))
        entry["view"] = view
        manifest["read_api"].append(entry)
        if entry["status"] != 200:
            manifest["errors"].append(f"{view}: HTTP {entry['status']}")
    entry = _write(read_dir / "kona_player_info.json",
                   _get(base, {"view": "kona_player_info"}, cookies,
                        {"X-Fantasy-Filter": json.dumps(PLAYER_FILTER)}))
    entry["view"] = "kona_player_info"
    manifest["read_api"].append(entry)
    if entry["status"] != 200:
        manifest["errors"].append(f"kona_player_info: HTTP {entry['status']}")
    entry = _write(read_dir / "leagueHistory.json",
                   _get(f"{READS_HOST}/apis/v3/games/ffl/leagueHistory/{league_id}",
                        {"view": ["mSettings", "mTeam", "mDraftDetail"],
                         "seasonId": season - 1}, cookies))
    entry["view"] = "leagueHistory"
    manifest["read_api"].append(entry)
    if entry["status"] != 200:
        manifest["errors"].append(f"leagueHistory: HTTP {entry['status']}")

    if init_b64 is None and team_id is not None:
        init_b64, socket_lines = espn_live.fetch_init_b64(league_id, season, team_id, swid,
                                                          espn_s2)
        lines = [(manifest["taken_at_ms"], ln) for ln in socket_lines]
        manifest["live_source"] = "fresh socket snapshot"
    elif init_b64 is not None:
        manifest["live_source"] = "running watch"
    else:
        manifest["live_source"] = "none: no watch and no team_id"
    if init_b64 is not None:
        current = _write_live(live_dir, manifest, init_b64, lines or [])
        report = _reconcile(read_dir, current)
        (live_dir / "reconcile.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        manifest["live"].append({"file": "reconcile.json", "as_of": "now",
                                 "status": report["status"]})
        manifest["reconcile"] = {k: report[k] for k in
                                 ("status", "live_picks", "read_api_picks", "detail")
                                 if k in report}
        if report["status"] == "mismatch":
            manifest["reconcile"]["differences"] = (len(report["missing_from_read_api"])
                                                    + len(report["missing_from_live"])
                                                    + len(report["disagreements"]))
        if report["status"] in ("mismatch", "unreadable"):
            manifest["errors"].append(f"reconcile against mDraftDetail: {report['status']}")
    else:
        manifest["reconcile"] = {"status": "no live state"}

    (root / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def _write_live(live_dir: Path, manifest: dict, init_b64: str,
                lines: list[tuple[int, str]]) -> list[dict]:
    """Write the live section and return the current pick list.

    `init.json` and `picks.json` are the join snapshot and never move; the draft
    as it stands now is `state.json`. At the pick that named this defect the two
    differed by eight picks, and nothing in the dump said which number was which.
    """
    init = espn_live.decode_init(init_b64)
    slot = espn_live.slot_by_team(init)
    (live_dir / "init.b64").write_text(init_b64, encoding="utf-8")
    manifest["live"].append({"file": "init.b64", "as_of": "join", "bytes": len(init_b64)})
    decoded = dataclasses.asdict(init)
    (live_dir / "init.json").write_text(json.dumps(decoded, indent=1, default=str),
                                        encoding="utf-8")
    joined = [{**p, "draft_slot": slot.get(p["team_id"])} for p in espn_live.picks_from_init(init)]
    manifest["live"].append({"file": "init.json", "as_of": "join", "picks": len(joined)})
    (live_dir / "picks.json").write_text(json.dumps(joined, indent=1), encoding="utf-8")
    manifest["live"].append({"file": "picks.json", "as_of": "join", "picks": len(joined)})

    wire = [line for _ts, line in lines]
    current = [{**p, "draft_slot": slot.get(p["team_id"])}
               for p in espn_live.replay_picks(init, wire)]
    (live_dir / "state.json").write_text(json.dumps(current, indent=1), encoding="utf-8")
    events = [e for e in (espn_live.pick_event(line) for line in wire) if e is not None]
    unparsed = [e for e in events if e["event"] == "unparsed"]
    manifest["live"].append({"file": "state.json", "as_of": "now", "picks": len(current),
                             "picks_at_join": len(joined),
                             "events_applied": len(events) - len(unparsed),
                             "events_unparsed": len(unparsed)})
    if unparsed:
        manifest["errors"].append(
            f"state.json: {len(unparsed)} pick events in lines.jsonl could not be parsed")

    # The queue is live state too, and the only place it exists is the event log:
    # INIT does not carry it and the read API never sees it. It is its own file
    # because a DRAFT_LIST changes nobody's picks -- folding it into the pick
    # reducer would move pick numbers that ESPN did not move.
    queue = espn_live.queue_from_lines(wire)
    echoes = sum(1 for line in wire if line.split(" ")[0] == "DRAFT_LIST")
    (live_dir / "queue.json").write_text(json.dumps(
        {"queue": queue, "echoes": echoes,
         "note": None if queue is not None else
                 "ESPN sent no DRAFT_LIST on this connection; the queue is unknown, "
                 "which is not the same as empty"}, indent=1), encoding="utf-8")
    manifest["live"].append({"file": "queue.json", "as_of": "now", "echoes": echoes,
                             "players": None if queue is None else len(queue)})

    with (live_dir / "lines.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for ts, line in lines:
            fh.write(json.dumps({"ms": ts, "line": line}) + "\n")
    manifest["live"].append({"file": "lines.jsonl", "as_of": "now", "rows": len(lines)})
    return current


def _reconcile(read_dir: Path, current: list[dict]) -> dict:
    """Check the replayed live state against the read API's own pick list.

    The read API is blind while a draft runs: `mDraftDetail` returns every slot
    in the draft order with `playerId` -1 and fills them in only once `drafted`
    turns true. Measured on a real mid-draft dump: 224 rows, 0 filled, against
    130 live picks. That is `status: blind`, not a disagreement -- calling it one
    would report every live pick as missing for the whole draft, which is the
    same as reporting nothing.
    """
    out: dict = {"file": "mDraftDetail.json", "live_picks": len(current)}
    try:
        data = json.loads((read_dir / "mDraftDetail.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {**out, "status": "unreadable", "detail": f"{type(exc).__name__}: {exc}"}
    detail = data.get("draftDetail") if isinstance(data, dict) else None
    if not isinstance(detail, dict):
        return {**out, "status": "unreadable", "detail": "no draftDetail object in the response"}
    rows = detail.get("picks") or []
    read_api = {p["overallPickNumber"]: p for p in rows
                if isinstance(p, dict) and p.get("overallPickNumber") is not None
                and p.get("playerId") not in (None, -1)}
    out |= {"read_api_rows": len(rows), "read_api_picks": len(read_api),
            "drafted": detail.get("drafted"), "in_progress": detail.get("inProgress")}
    if not read_api:
        return {**out, "status": "blind",
                "detail": "every slot came back with playerId -1; the read API fills "
                          "picks in only once the draft completes"}
    live = {p["overall"]: p for p in current}
    disagree = [{"overall": n,
                 "live": {"player_id": live[n]["player_id"], "team_id": live[n]["team_id"]},
                 "read_api": {"player_id": read_api[n].get("playerId"),
                              "team_id": read_api[n].get("teamId")}}
                for n in sorted(set(live) & set(read_api))
                if (live[n]["player_id"], live[n]["team_id"])
                != (read_api[n].get("playerId"), read_api[n].get("teamId"))]
    missing_from_read_api = sorted(set(live) - set(read_api))
    missing_from_live = sorted(set(read_api) - set(live))
    return {**out,
            "status": "clean" if not (disagree or missing_from_read_api or missing_from_live)
                      else "mismatch",
            "missing_from_read_api": missing_from_read_api,
            "missing_from_live": missing_from_live,
            "disagreements": disagree}
