"""Everything ESPN will say about one league's draft, written to disk as-is.

Two surfaces. The read API (`lm-api-reads`) answers one JSON document per
`view`; each is saved untouched under `read_api/`. The draft-room socket's INIT
snapshot is saved raw (base64) and decoded under `live/`, with every line the
watch has seen since it joined, timestamped, so pick timing exists somewhere.
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
    return {"SWID": swid if swid.startswith("{") else f"{{{swid}}}", "espn_s2": espn_s2}


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

    base = f"{READS_HOST}/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league_id}"
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
        _write_live(live_dir, manifest, init_b64, lines or [])

    (root / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def _write_live(live_dir: Path, manifest: dict, init_b64: str,
                lines: list[tuple[int, str]]) -> None:
    init = espn_live.decode_init(init_b64)
    (live_dir / "init.b64").write_text(init_b64, encoding="utf-8")
    manifest["live"].append({"file": "init.b64", "bytes": len(init_b64)})
    decoded = dataclasses.asdict(init)
    (live_dir / "init.json").write_text(json.dumps(decoded, indent=1, default=str),
                                        encoding="utf-8")
    manifest["live"].append({"file": "init.json", "picks_made": len(espn_live.picks_from_init(init))})
    slot = espn_live.slot_by_team(init)
    picks = [{**p, "draft_slot": slot.get(p["team_id"])} for p in espn_live.picks_from_init(init)]
    (live_dir / "picks.json").write_text(json.dumps(picks, indent=1), encoding="utf-8")
    manifest["live"].append({"file": "picks.json", "rows": len(picks)})
    with (live_dir / "lines.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for ts, line in lines:
            fh.write(json.dumps({"ms": ts, "line": line}) + "\n")
    manifest["live"].append({"file": "lines.jsonl", "rows": len(lines)})
