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

`kona_player_info` with an `X-Fantasy-Filter` header carries ESPN's own market for every
player: `player.ownership.averageDraftPosition`, `percentOwned`, `percentStarted`, and
`player.draftRanksByRankType.PPR.rank`. `board.load_espn_adp` reads it and, when
`ESPN_LEAGUE_ID` and the cookies are set, it is the board's ADP (`adp_source: espn`)
because it is the list ESPN opponents draft from; FantasyPros consensus is a different
market and stays the fallback.

`averageDraftPosition` is **not null for a player nobody drafts** — it is filled with a
placeholder near ESPN's default draft length. On the 2026 list, 823 of 999 rows land in
one 4-pick-wide bin: 260 share exactly 169.99 and 208 share exactly 170.00. A value 468
players share cannot be a draft position (only one player is taken at each pick), and the
rows carrying it have a median 0.03% roster rate against 86.7% for the rest. Read as a
pick number it tells the survival model that most of the board is about to disappear:
`plan_my_draft`'s availability filter treated all 448 such board rows as already gone once
the pick number passed ~174, collapsing the pool from 501 candidates at pick 164 to nine
at pick 189.

`board.undrafted_adp_mask` finds the placeholder rather than hardcoding it, since its
value follows ESPN's default draft length: the most-repeated ADP, accepted only when more
than `UNDRAFTED_MIN_TIES` (20) players share it, plus a `UNDRAFTED_ADP_TOLERANCE` (1.0
pick) run either side to catch the smear the averaging leaves across neighbouring
hundredths. That width is load-bearing, not decorative: 468 rows sit exactly on 169.99 or 170.00
and another 326 are caught only by the tolerance. Those 326 are the ones worth
checking, and the check is whether their ADP carries any signal. It does not. Among
the 205 rows outside the band, ADP and ESPN's own rank correlate at rho = +0.95, and
mean ADP rises 47.7 -> 127.8 -> 159.3 -> 168.6 across rank buckets. Inside the band
rho = +0.09, the whole spread is 0.18 picks against 53.7 outside, and mean ADP across
rank buckets 199-400, 400-900, 900-1500 and 1500-2500 is 170.18, 169.93, 169.98,
169.99 — not even monotone. In the tolerance-only subset rho = -0.29: a better-ranked
player has a *later* ADP there, which is the opposite of a draft position. Shrinking
the tolerance to absorb float noise alone would leave those 326 rows carrying a number
that runs backwards against rank.

Ownership is a separate matter and is not touched. Some of the 326 are real players
rostered in 15-34% of leagues (Cairo Santos, Tre Tucker, Pat Freiermuth), against a
maximum of 0.51% among the rows sitting exactly on the placeholder. What is claimed
about them is only that ESPN's ADP does not price them, which the correlations above
show is true of every row in the band. A market frame with continuous ADPs (a pasted
CSV, consensus ECR) trips neither condition and is never touched.

Those rows keep their `espn_proj` and `espn_rank` and are priced by the same synthetic
fallback that covers a row the market join missed, under `adp_source: undrafted` so the
two causes stay apart in `draft_audit`'s `market_join` block (449 undrafted against 9
genuinely unjoined on the live board).

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

### As-of market snapshots

None of ESPN's surfaces answers "what was this player's ADP when pick 87 was
made". `ownership.averageDraftPosition`, `draftRanksByRankType.PPR.rank` and the
projection all move: ADP moves as other leagues draft, the projection moves with
news. A replay run days later therefore scores every pick against prices nobody
in the room had.

The watch is the only process present while the draft runs, so it records them.
`watch.write_snapshot` writes the market for the players still available to

```
~/.ffdraft/state/snapshots_<league_id>/<pick>.parquet
```

where `<pick>` is the pick then on the clock — so `115.parquet` is the board as
it stood when pick 115 was made, not after it.

It is written at three points. After `INIT`, as the seed. After each `SELECTED`,
so a file exists even if nothing else arrives. And again on `SELECTING`, which is
ESPN naming the team that has just gone on the clock and is therefore the event
this actually wants — a state named by the server rather than inferred after a
different one. The `SELECTING` write rewrites the same pick's file with a fresher
board, and because ESPN sends a new `SELECTING` when the clock reopens after an
`UNDONE`, the rewrite self-corrects. `UNDONE` also deletes the files for the
rolled-back picks: a snapshot of a board state the draft backed out of would
otherwise let a later replay price a pick from a world that did not happen.

The `SELECTING` line names a team and the file is numbered from the watch's own
pick count — two sources for one fact. When they disagree the write is skipped
and logged, leaving the `SELECTED`-anchored file in place: a snapshot filed
under the wrong pick number is the silent corruption the whole feature exists to
avoid.

Each write re-reads the board through the watch's `refresh` callback first.
`DraftWatch.board` is otherwise the board the watch was *constructed* with, and
the only other thing that refreshes it runs on the handful of picks near your own
turn — so without this every file would hold identical ADP while the coverage
block below reported success. `server.watch_draft`'s refresh is a cache lookup,
so the per-pick cost is a dict hit.

| column | from | why |
|---|---|---|
| `_key` | `names.normalize(name)` | what the board and the recorded picks join on |
| `player_id` | board `player_id` (nflverse gsis) | a stable id alongside the name key |
| `adp` | board `adp` (ESPN or consensus, per `adp_source`) | the survival model's input |
| `espn_rank` | ESPN PPR draft rank | the choice model's `espn_list` feature |
| `espn_proj` | ESPN season projection | `model.role_multiplier` |

Bounds: `watch.SNAPSHOT_ROWS` (300) rows per file, cheapest ADP first, so a full
14-round 16-team draft writes 224 files of a few hundred rows — single-digit
megabytes. Only the market columns are kept; projections, roles and consistency
are the model's own and were never moving ESPN numbers. Nothing prunes a
league's directory afterwards; `watch.drop_snapshots_above` exists for the
`UNDONE` case and is the piece to reuse if that is ever wanted.

A failed write is logged and dropped, never raised — the socket loop must not
lose a pick to a snapshot — but it is not silent: the first failure pushes a
`snapshot_failed` channel event, and then it stays quiet until a write succeeds
again. `draft_room` and `stop_watch` both report `as_of_snapshots` next to
`picks_seen`, so "picks_seen 122, as_of_snapshots 0" is one line to read.

`replay.replay_draft(as_of=True, snapshots=<league id or directory>)` reads them
back, overwriting those three columns for the rows a snapshot covers before each
pick is scored. Coverage is reported rather than assumed, in the answer's `as_of`
block: how many picks had a snapshot at all (none exist before the watch first
connected), the first and last that did, the mean share of the pool each one
reached, and how often the player actually taken was inside it. Every per-pick
row also carries `as_of` (was this pick's own player priced from the snapshot)
and `as_of_pool_share`, because a reader scanning the rows will not cross-check
the summary. Anything uncovered keeps today's numbers.
`draft_replay(league_id=..., as_of=true)` exposes it.

### Draft history: what ESPN keeps

Enumerated 2026-09-04 from the kona bundles (draft, draftrecap, history pages) and the
read API. No surface carries a timestamp per pick or a replay of room presence.

| surface | has | lacks |
|---|---|---|
| socket commands (all 19 literals in the communicator) | ADJUST ASSIGN AUTODRAFT AUTO_NOMINATION BID CENSOR CHAT DRAFT_LIST JOIN LEAVE NOMINATE PAUSE PING PRENOMINATE RESET ROUTE SELECT SET UNDO | any history/replay request |
| INIT snapshot | pick number, team, player, slot, keeper, autodraft type, selector profile id (0 in practice) | timestamps |
| `mDraftDetail`, `kona_draft_detail` | 13 pick keys: autoDraftTypeId bidAmount id keeper lineupSlotId nominatingTeamId overallPickNumber playerId reservedForKeeper roundId roundPickNumber teamId tradeLocked | timestamps; `playerId` -1 mid-draft |
| `draftInit` | pick `id`, `teamId` only | everything else |
| draft recap page | `mDraftDetail`, `mSettings`, `mTeam` | anything beyond those |
| `/communication/?view=kona_league_communication` | complete feed (count header == topics returned); ACTIVITY_SETTINGS and ACTIVITY_STATUS topics with ms dates | pick events, at least while the draft runs |
| room chat | replayed on every join with ms timestamps | nothing |

Open: the client also knows `ACTIVITY_TRANSACTIONS` and `ACTIVITY_SCHEDULE` topic types;
none exist in this league yet. Whether completed drafts post picks as transactions is
untested until a draft completes.

Pick timing and presence over time exist only in a client that was connected; the watch
records both from connect onward, and `dump_draft` writes them out (`live/lines.jsonl`).
The join burst after `INIT` is `TOKEN`, `CLOCK`, `AUTOSUGGEST <playerId>` (ESPN's own
suggestion for your next pick), then the chat replay. `leagueHistory/<id>` answers 404
for this league with or without `seasonId`: a first-season league has no history there.

### Players the board cannot model

The board holds QB, RB, WR and TE with a modelled season. Recorded picks outside that
set are counted on your roster by the position ESPN's crosswalk gives them, and
`draft_audit` lists them as warnings. Seen in a live draft on 2026-09-04:

| pick | why absent |
|---|---|
| Brandon Aubrey, Denver D/ST | K and DST are not modelled |
| MarShawn Lloyd, Jonathon Brooks | no game since 2024, nothing to project |
| Travis Hunter | nflverse position CB (7 weeks in 2025); ESPN lists him WR. Two-way players are not handled |

The SSE variant `https://fantasydraft.espn.com/game-1/league-{league_id}/sse/JOIN?...`
answers `ERROR 1 No team with ID {team_id} found` for the same parameters.

Source: ESPN kona bundle `_next/.../page/football/draft.js` build `2d26c1207d60-1.487`.
