# Data sources

Every external surface the server touches, what the code expects from it, and its
state as of 2026-09-04. Re-run `just surfaces` to re-probe.

## nflverse releases

`https://github.com/nflverse/nflverse-data/releases/download/<tag>/<asset>`. One asset
per season for in-season data; the season's asset appears once games are played, so
the current season 404s until Week 1 and `sources.py` skips it with a printed `!` line.

| tag | asset | used by | 2025 | 2026 |
|---|---|---|---|---|
| `stats_player` | `stats_player_week_{season}.parquet` | weekly stats, points, finishes | yes | 404 |
| `player_stats` | `player_stats_{season}.parquet` | legacy fallback, last season 2024 | n/a | n/a |
| `snap_counts` | `snap_counts_{season}.parquet` | role, routes estimate | yes | 404 |
| `injuries` | `injuries_{season}.parquet` | injury history | yes | 404 |
| `weekly_rosters` | `roster_weekly_{season}.parquet` | birth dates, espn_id / sleeper_id crosswalk | yes | yes |
| `players` | `players.parquet` | master player table | updated daily | |
| `depth_charts` | `depth_charts_{season}.parquet` | current team override | yes | yes |
| `pbp` | `play_by_play_{season}.parquet` | O-line, pace, defense, red zone | yes | 404 |
| `nextgen_stats` | `ngs_receiving.parquet`, `ngs_rushing.parquet` | separation, cushion, YAC | all seasons in one file | |
| `draft_picks` | `draft_picks.parquet` | rookie draft capital | 2026 class present | |
| `combine` | `combine.parquet` | rookie athleticism | | |

Unused assets in the same releases: `stats_player_reg/post/regpost_{season}` season
aggregates, `stats_team_*`, `player_stats_def_*`, `player_stats_kicking_*`.

## nfldata

`https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv`: schedule
with `div_game`. 2026 season present (272 games).

## FantasyPros consensus rank (via dynastyprocess)

`https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_fpecr.parquet`.
Columns used: `page_type`, `player`, `pos`, `tm`, `ecr`, `sd`, `best`, `worst`,
`scrape_date`. `redraft-overall` and `redraft-op` (superflex) scraped daily; latest
scrape 2026-09-04. This is the ADP source for the live board (`adp_source: consensus`).

## FantasyPros ADP page

`https://www.fantasypros.com/nfl/adp/{ppr-overall,half-point-ppr-overall,overall}.php`.
Third-choice fallback in `board.load_adp`. The ADP table is rendered client-side; the
only server-rendered table is the sources legend, so this path raises and the board
falls back to model rank. Do not rely on it.

## Sleeper

`https://api.sleeper.app/v1/draft/{draft_id}/picks`, public, no credentials. Liveness
check: `https://api.sleeper.app/v1/state/nfl`.

## ESPN league read API

`https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league_id}`
with `view` in `mDraftDetail`, `mTeam`, `mSettings`, `kona_player_info`. Private leagues
need the `SWID` and `espn_s2` cookies (`ESPN_SWID`, `ESPN_S2`).

Fields used: `settings.scoringSettings.scoringItems[statId=53].points` (reception
points), `settings.rosterSettings.lineupSlotCounts` (slot id -> count; 0 QB, 2 RB, 4 WR,
6 TE, 16 DST, 17 K, 20 bench, 21 IR, 23 flex), `teams[].owners`, `draftDetail.picks[]`
with `overallPickNumber`, `roundPickNumber`, `teamId`, `playerId`.

During a live draft this API is blind: `draftDetail.inProgress` is true, every pick has
`playerId: -1`, every roster is empty, and `kona_player_info` with `filterStatus: ONTEAM`
returns nothing. Picks appear only once `draftDetail.drafted` is true.

## ESPN live draft socket

The draft room gets its state over a websocket. `espn_live.py` speaks it.

1. Token: `GET .../leagues/{league_id}/teams/{team_id}/draftSecurity` with the cookies and
   `X-Fantasy-Source: kona` returns an integer. 401 for a team the SWID does not own.
   Draft token is `1:{league_id}:{team_id}:{SWID}:{integer}` (1 is ESPN's fantasyGameId
   for football).
2. Connect: `wss://fantasydraft.espn.com/game-1/league-{league_id}/JOIN?1=1&2={league_id}&3={team_id}&4={SWID}&5={token}&6=false&7=false&8=KONA&nocache=N`
   with `Cookie: SWID=..; espn_s2=..` and `Origin: https://fantasy.espn.com`.
3. Frames are newline-terminated, space-separated fields. First frame `INIT <base64>` is a
   binary snapshot (big-endian ints, versioned transcoders) holding league, teams, rosters
   and all 224 pick slots. Then `TOKEN`, `CLOCK phase ms teamId playerId amount`,
   `SELECTED teamId playerId slotId`, `SELECTING teamId secs`, `CHAT`, `JOINED`, `LEFT`,
   `STATE`, `UNDONE pick`, `PONG`, `ERROR severity text`. Client sends `PING <ms>`,
   `LEAVE`, and `SELECT <playerId>` to make its pick (answered by `SELECTED`, or
   `ERROR` when it is not that team's turn).
4. Past picks are only in INIT; the server does not replay `SELECTED`. `JOINED`, `LEFT`
   and `CHAT` carry team id, owner SWID and (for chat) a millisecond timestamp, so a
   long-lived listener can log draft-room presence and chat per owner. Not built.
5. One connection per team and member, regardless of device cookies (tested with the
   browser's full cookie jar). A new join closes the existing one: the older side sees
   `LEFT <team> <swid> 2` then the socket drops without a close frame; a browser shows
   "Duplicate Connection". `watch.py` treats that LEFT as a pause signal.

The SSE variant `https://fantasydraft.espn.com/game-1/league-{league_id}/sse/JOIN?...`
answers `ERROR 1 No team with ID {team_id} found` for the same parameters.

Source: ESPN kona bundle `_next/.../page/football/draft.js` build `2d26c1207d60-1.487`.
