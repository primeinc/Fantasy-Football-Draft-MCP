# Changelog

All notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[semantic](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

**ESPN live draft sync**
- `sync_draft(platform="espn")` now works while the draft is running. The read API
  returns no picks until a draft completes, so `board.sync_espn` joins the draft
  room socket (`espn_live.py`), decodes the INIT snapshot, and returns every pick
  with the team's real draft position as its slot. Needs `ESPN_SWID`/`ESPN_S2`
  and a team you own; the browser draft room reconnects after a brief
  "Duplicate Connection" dialog. Protocol in `docs/data-sources.md`.
- `watch_draft` / `stop_watch`: hold the draft room socket open and push each
  pick into the Claude Code session as a channel message (`watch.py`), with a
  recommendation once the user is within three picks. The server declares the
  `claude/channel` capability on both handshake eras; the session must start with
  `claude --dangerously-load-development-channels server:fantasy-draft`.
  ESPN allows one draft-room connection per team, so the watch pauses when the
  browser room is opened instead of fighting it.
- `make_pick`: draft a player over the watch's socket (`SELECT <playerId>`), so
  the browser room is not needed at all.
- `draft_room`: who is in the draft room and the latest chat, tracked by the watch
  from the snapshot's online flags and the JOINED/LEFT/CHAT lines.
- `draft_queue` / `set_draft_queue`: read and replace the ESPN pick queue over the
  socket (`DRAFT_LIST`), so autopick has your plan if you miss the clock.
- `board.resolve_espn_id`: name -> ESPN id through the board, the crosswalk
  (kickers), and team defenses; used by `make_pick` and `set_draft_queue`.
- The watch re-reads the board and bye weight for every pushed recommendation;
  `sync_draft` refuses while a watch is connected; `just watch <league_id>` runs
  the watch standalone with a log file.

**Draft audit**
- `draft_audit` checks the invariants between board, draft state and
  recommendation (key freshness, contiguous picks, no duplicates, your picks on
  your slot, no drafted player recommended). `sync_draft` returns the same
  block; the watch pushes `audit_failed` after a snapshot that breaks one.
  Added after two live incidents: dotted-initial names keyed differently on
  the board and in the state, and a cached board keeping stale keys.

**ESPN ADP**
- `board.load_espn_adp`: ESPN's own average draft position from
  `kona_player_info` (`ownership.averageDraftPosition`, with ESPN id, PPR rank
  and percent owned). `load_adp` prefers it over consensus when
  `ESPN_LEAGUE_ID` and the cookies are set, so survival odds are priced off
  the list the room actually drafts from. Boards carry `adp_source`
  (`espn` / `consensus` / `modelled`); a board cached under consensus is
  repriced in place on the next load (`server._price_board`).

**Room order and team strength**
- `draft_room.upcoming`: the next five picks with team and owner names, room
  presence and whether the pick is yours (`DraftWatch.upcoming`).
- `draft_strength`: every team's draft ranked by projected starter points
  (`board.team_strength`), with bench projection and open starter slots.

**ESPN projections as a role check**
- `load_espn_adp` also carries `espn_proj` (ESPN's season projection under the
  league's scoring: stats entry statSourceId 1, scoringPeriodId 0) and
  `espn_injury`; `attach_adp` puts both on the board. `model.role_multiplier`
  scales `pick_value` by ESPN/model when ESPN projects under 70% of the model
  (floor 0.2); `explain` reports the ESPN number and the scaling; `audit_state`
  warns on recommended players in that state or with no ESPN projection.
  Found live: the model had Tyrone Tracy Jr. RB22 at 185 points from 2025
  box scores while ESPN projected 41, a backup on the 2026 depth chart.
- `league_rules` reads `pointsOverrides`: the D/ST points-allowed and
  yards-allowed bands carry `points` 0 and their real values per slot 16.
  Kicker and D/ST statIds are named from the espn-api map.

**Draft dump**
- `dump_draft` tool and `just dump <league_id> [out_dir]` (`espn_dump.py`):
  every read-API view as its own JSON file, the full player pool, league
  history, the draft room's INIT payload raw and decoded, the picks, and a
  timestamped log of every socket line the watch received. `DraftWatch`
  now keeps `init_b64` and `lines` for it; `espn_live.fetch_init_b64` returns
  the payload undecoded. Output is gitignored (`espn_dump_*/`).

**League rules**
- `league_rules`: the league's settings as ESPN states them (`mSettings`):
  draft, roster slots and position limits, every scoring item, schedule and
  playoffs, waivers, trades, lineup lock, tiebreakers, plus the season's
  bye-week topology. Replaces the assumed-template unknowns.

**Bye weeks**
- Every board row carries `bye_week` from the nfldata schedule
  (`features.team_bye_weeks`). `who_should_i_pick` and the watch report
  `bye_week` and `bye_conflicts` (the players you hold who share it), and
  `explain` says so.
- New weight `bye` (`model_settings(bye_weight=...)`, default 0): cuts a
  candidate's pick_value by that fraction per same-position player you hold on
  the same bye and half that per other player. A stacked bye costs a week of a
  starter, about 1/14 of a season.
- `bye_backtest`: paired mock drafts with and without the penalty, scored on
  weekly best lineups from real box scores (`adp.weekly_lineup_points`,
  `adp.best_weekly_lineup`). The mock-draft trial loop is shared
  (`adp._draft_trial`). A 2022-2025 run (12 paired drafts per season, weight
  0.08) gave +5.8, +7.0, -19.2, -2.0 weekly points, -2.1 overall: fewer empty
  starter slots in three of four seasons, swamped by who gets drafted instead.
  `bye` therefore stays 0 by default and the conflicts stay informational.
- `docs/data-sources.md`: every external endpoint, fields used, state at
  2026-09-04. `just surfaces` re-probes them.

### Fixed

- ESPN picks of current-year rookies resolved as `ESPN#<id>`: the id crosswalk only
  read weekly rosters for the lookback seasons. It now adds players from the
  nflverse `players` table that the rosters lack.
- `parse_pasted_board` dropped names with dotted initials (A.J. Brown, T.J.
  Hockenson, J.K. Dobbins), shifting every later pick, and mangled
  "Round N, Pick M - Name" because the comma split ran before the prefix strip.
- FantasyPros ADP HTML fallback crashed under pandas 3 (`read_html` no longer
  accepts a literal string) and, once fixed, returned nothing: the page renders
  its table client-side. It now raises with a clear message so the board falls
  back to model rank visibly.

**Team drive efficiency and red zone identity**
- New `features.team_drive_efficiency` (share of a team's drives ending in a
  touchdown/field goal/punt) and `features.redzone_identity_shift` (a team's neutral
  pass rate minus its red zone pass rate), surfaced through `team_context`.
- Both are informational only, like `matchup_z` in `separation_report` — not folded
  into `draft_score`. New `redzone_shift_backtest` tool tested whether blending the
  identity shift into the touchdown-luck signal (`m_td_luck`) improves prediction of
  next-season points; a 2022-2025 run found it makes predictions *worse* for both WR
  (`improvement_corr` -0.006, 300 player-seasons) and TE (-0.053, 117 player-seasons),
  so it stays informational, matching the conclusion `matchup_backtest` already
  reached for schedule difficulty.
- `team_drive_efficiency` needs new play-by-play columns (`drive`,
  `fixed_drive_result`) not present in a `play_by_play` cache built before this
  change — run `refresh_data(force_download=true)` if it comes back empty.

**Touchdown luck**
- New environment multiplier (`m_td_luck`, weight `td_luck` / `td_luck_weight` in
  `model_settings`, default 0.06): a player's red zone touch/touchdown rate, from raw
  play-by-play, regressed toward what his position converts on average — computed
  from the same starter-caliber cohort the baseline projection regresses toward.
  Below 8 red zone touches a rate is treated as noise and sits neutral.
- `player_report` now shows `rz_touches`, `rz_td`, `rz_td_rate`, `rz_baseline_rate`
  alongside the multiplier, and `explain()`'s plain-language summary surfaces it as
  "touchdown regression" whenever it's non-trivial.
- New `features.player_redzone_role` (raw plays → per-player-season red zone
  touches/TDs) and `model.touchdown_luck_multiplier` (the bounded z-score
  adjustment, independently unit-tested).

## [1.0.0] — 2026-08-12

First release. A live fantasy football draft analyst exposed over MCP.

### Added

**Draft recommendations**
- `who_should_i_pick` weighs projected value, week-to-week consistency, open starting
  slots, and the odds each player survives to your next pick.
- Positional opportunity cost rather than raw value: what matters is the marginal gain
  over what a position still offers at your next turn, not who grades highest overall.
- `plan_my_draft` simulates all your picks from your slot under balanced, zero-RB,
  hero-RB or robust-RB strategies.

**Projections**
- Recency-weighted production across five seasons, converted to your exact scoring.
- Offensive line from adjusted line yards and pressure allowed per dropback.
- Neutral-script pace and run/pass split, measured only between 20% and 80% win
  probability so garbage time doesn't distort it.
- Five-year defensive strength by position defended, plus divisional schedule weighting.
- Injury risk from availability history, injury-report frequency and workload burden.
- Positional aging curves.

**Separation and route efficiency**
- NGS tracking separation and cushion, plus estimated YPRR and TPRR, as an open-data
  stand-in for paywalled charting. Man/zone splits are not reproducible.

**Rookies**
- Projected from draft capital, fitted to ten years of first-year outcomes and blended
  with empirical pick bins so the curve can't extrapolate past the data.

**Scoring formats**
- PPR, half PPR, standard, superflex and TE premium. Consensus rankings are converted
  between formats, since only PPR is published upstream.

**Multiple leagues**
- Named leagues with separate boards, replacement levels and in-progress drafts.

**Platform sync**
- Sleeper (public API), ESPN (public leagues, or private with cookies), or paste from
  anywhere.

**Name resolution**
- Aliases, nicknames, suffixes, punctuation, bare surnames, initialisms and typos.
  Ambiguous names name their candidates rather than guessing.

**Analysis**
- `draft_value_history` backtests consensus rank against actual finish across 913
  draftable player-seasons.
- `value_picks`, `separation_report`, `defense_report`, `team_context`, `player_report`,
  `compare_players`, `rookie_report`.

### Notes on accuracy

Several defaults were calibrated against data rather than assumed, and the reasoning is
documented in `docs/methodology.md`:

- Baseline projections regress toward the mean of *starter-caliber* players, not all
  players. Regressing toward a mean that includes third-stringers cut genuine starters
  by roughly a third.
- Consistency is regressed for small samples, so a backup with three good games doesn't
  outrank proven starters on reliability he never demonstrated.
- Rookie curves are capped at each pick bin's observed 75th percentile.
- Format conversion is damped to 0.6, because draft rooms move less than pure points
  arithmetic implies.
