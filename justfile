set minimum-version := '1.55.0'
set default-list
set script-interpreter := ['.venv/Scripts/python.exe']

python := '.venv/Scripts/python.exe'

# Create .venv and install the package with dev extras
setup:
    uv venv .venv --python 3.12
    uv pip install --python {{ python }} -e ".[dev]"

# Lint, type-check, and run the offline test suite
check:
    {{ python }} -m ruff check src tests
    uvx ty check src tests
    {{ python }} -m pytest tests -q

# One-time nflverse download and board build (cache in ~/.ffdraft)
data:
    {{ python }} setup_data.py

# Run the MCP server on stdio (what Claude launches)
serve:
    {{ python }} -m ffdraft.server

# Probe every external data surface; see docs/data-sources.md
[script]
surfaces:
    import io
    import json
    import os

    import pandas as pd
    import requests

    UA = {"User-Agent": "ffdraft-mcp/1.0"}
    season = int(os.environ.get("FFDRAFT_SEASON", "2026"))
    nflverse = "https://github.com/nflverse/nflverse-data/releases/download"

    def head(url):
        try:
            return requests.head(url, allow_redirects=True, timeout=20, headers=UA).status_code
        except requests.RequestException as exc:
            return type(exc).__name__

    print("== nflverse per-season assets")
    for tag, tmpl in (
        ("stats_player", "stats_player_week_{s}.parquet"),
        ("snap_counts", "snap_counts_{s}.parquet"),
        ("injuries", "injuries_{s}.parquet"),
        ("weekly_rosters", "roster_weekly_{s}.parquet"),
        ("depth_charts", "depth_charts_{s}.parquet"),
        ("pbp", "play_by_play_{s}.parquet"),
    ):
        for s in (season - 1, season):
            print(f"  {tag:<15} {tmpl.format(s=s):<34} {head(f'{nflverse}/{tag}/{tmpl.format(s=s)}')}")
    print("== nflverse single assets")
    for tag, name in (("players", "players.parquet"), ("draft_picks", "draft_picks.parquet"),
                      ("combine", "combine.parquet"), ("nextgen_stats", "ngs_receiving.parquet"),
                      ("nextgen_stats", "ngs_rushing.parquet")):
        print(f"  {tag:<15} {name:<34} {head(f'{nflverse}/{tag}/{name}')}")

    print("== nfldata games.csv")
    g = pd.read_csv("https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv")
    print(f"  seasons {int(g.season.min())}-{int(g.season.max())}, {season} games {int((g.season == season).sum())}, div_game {'div_game' in g.columns}")

    print("== dynastyprocess ECR")
    e = pd.read_parquet("https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_fpecr.parquet",
                        columns=["page_type", "scrape_date"])
    e["scrape_date"] = pd.to_datetime(e["scrape_date"])
    for pt in ("redraft-overall", "redraft-op"):
        sub = e[e["page_type"] == pt]
        print(f"  {pt:<16} latest scrape {sub['scrape_date'].max().date()}  rows {int((sub['scrape_date'] == sub['scrape_date'].max()).sum())}")

    print("== sleeper")
    r = requests.get("https://api.sleeper.app/v1/state/nfl", timeout=20, headers=UA)
    print(f"  {r.status_code} season {r.json().get('season')} week {r.json().get('week')}")

    print("== fantasypros ADP page (server-rendered player table?)")
    r = requests.get("https://www.fantasypros.com/nfl/adp/ppr-overall.php", timeout=30,
                     headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
    tables = pd.read_html(io.StringIO(r.text))
    has_player = any(any("player" in str(c).lower() for c in t.columns) for t in tables)
    print(f"  {r.status_code} tables {len(tables)} player table {has_player}")

    print("== ESPN league read API")
    swid, s2 = os.environ.get("ESPN_SWID"), os.environ.get("ESPN_S2")
    league, team = os.environ.get("ESPN_LEAGUE_ID"), os.environ.get("ESPN_TEAM_ID")
    if not (swid and s2 and league and team):
        print("  skipped: set ESPN_SWID, ESPN_S2, ESPN_LEAGUE_ID, ESPN_TEAM_ID (a team you own)")
    else:
        url = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league}"
        r = requests.get(url, params={"view": ["mDraftDetail", "mTeam", "mSettings"]},
                         cookies={"SWID": swid, "espn_s2": s2}, timeout=20, headers=UA)
        print(f"  {r.status_code}")
        if r.ok:
            d = r.json()
            dd = d.get("draftDetail") or {}
            picks = dd.get("picks") or []
            filled = sum(1 for p in picks if p.get("playerId") not in (None, -1))
            print(f"  drafted {dd.get('drafted')} inProgress {dd.get('inProgress')} picks {len(picks)} filled {filled} teams {len(d.get('teams') or [])}")
            tok = requests.get(f"{url}/teams/{team}/draftSecurity", cookies={"SWID": swid, "espn_s2": s2},
                               headers={**UA, "Accept": "application/json", "X-Fantasy-Source": "kona"}, timeout=20)
            print(f"  draftSecurity {tok.status_code} {json.dumps(tok.text[:40])}")

# Spawn the server as a real stdio subprocess, handshake, list tools, call two
[script]
smoke:
    import asyncio
    import os

    from mcp import Client, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = os.getcwd()
    server = StdioServerParameters(
        command=os.path.join(root, ".venv", "Scripts", "python.exe"),
        args=["-m", "ffdraft.server"],
        env={"FFDRAFT_SEASON": "2026", "PYTHONPATH": os.path.join(root, "src")},
    )

    async def main() -> None:
        # mode="legacy" is the initialize handshake Claude Code uses; "auto" is the
        # 2026 discover probe. The channel capability must show on both.
        for mode in ("legacy", "auto"):
            async with Client(stdio_client(server), mode=mode) as client:
                info = client.server_info
                print(f"{mode}: server {info.name if info else None} protocol "
                      f"{client.protocol_version} experimental "
                      f"{getattr(client.server_capabilities, 'experimental', None)}")
        async with Client(stdio_client(server), mode="legacy") as client:
            tools = (await client.list_tools()).tools
            print(f"tools: {len(tools)}")
            for t in tools:
                print("  ", t.name)
            for name, args in (("list_leagues", {}), ("best_available", {"limit": 5})):
                r = await client.call_tool(name, args)
                print(f"--- {name} is_error={r.is_error}")
                print(r.content[0].text)

    asyncio.run(main())
