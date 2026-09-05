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

**Draft replay and room drift**
- `draft_replay` (`replay.py`, `just replay`): every pick re-run through the
  model for the team on the clock; model rank of the real pick, points left
  on the table, reach against ADP, per-team totals, and the survival model's
  calibration and Brier score from the forecasts it made during the draft.
- `replay.room_drift`: median picks before ADP the room drafts, room-wide and
  per position (a position's own median once it has 8 picks). `recommend`
  takes `adp_shift` (one number or one per position); `who_should_i_pick`
  and the watch pass the per-position shift. Evidence from the live replay at
  122 picks, 1060 survival forecasts, Brier / log loss: no shift 0.140 /
  0.444; room median (4 picks) 0.136 / 0.433; per position 0.128 / 0.412;
  base rate 0.250. The room takes QBs a median 16 picks before ADP (Mahomes
  at 47, Goff at 65); QB survival Brier went from 0.272 (worse than the base
  rate) to 0.221.
- `role_multiplier` is continuous: ratio / 0.70 below 0.70 (floor 0.2),
  ratio / 1.30 above 1.30 (cap 1.3), 1 between, so a ratio of 0.699 and
  0.701 price within a hair of each other. Before/after on the live board at
  pick 125 (model proj, ESPN proj, multiplier): Tyrone Tracy 185 / 41 ->
  0.32; Oronde Gadsden 164 / 77 -> 0.67; KC Concepcion 102 / 156 -> 1.17;
  Aaron Jones 103 / 178 -> 1.30; Jakobi Meyers 205 / 182, Woody Marks
  177 / 132, Deebo Samuel 203 / 150 and Chuba Hubbard 160 / 180 -> 1.00.
  The 0.70 and 1.30 edges are policy, chosen so that ordinary
  model-vs-ESPN disagreement (most of the board sits between 0.75 and 1.25)
  is left alone; they are not fitted.
- Per-pick replay rows carry `pick_regret` (model pick_value left on the
  table), `choice_percentile`, `market_z` (reach in units of the survival
  model's ADP spread, `model.ADP_SD_FLOOR` / `ADP_SD_RATE`), `need_mult`,
  `role_mult` and `p_available_next`; overall adds log loss and survival
  calibration by round and by position.

**Same-name pairs and NaN projections in the replay**
- `replay_draft` indexed the board by normalised name and added the taken name
  to a set, so two board rows sharing a key were both removed when one was
  taken, and the second pick of that name read `off_board`. Harmless while the
  only duplicate was one player listed twice; not harmless now that the
  position-aware market join lets a genuine same-name pair coexist as two rows
  at different positions. It is keyed by board row, the same way
  `counterfactual_draft` is, and the row-resolution helpers (`_key_rows`,
  `_row_for`, `_rows_for_picks`) are shared between the two. A recorded pick
  carries a name, so a duplicate is settled by the pick's own position and by
  what earlier picks already took.
- Verified as a pure refactor on the live 632-row board: `just replay` returns
  identical numbers before and after (122 picks scored, 117 on board, Brier
  0.128, log loss 0.412, blend 3.107 / top1 0.188).
- `board.lineup_value` counted a NaN projection as NaN, not 0: NaN is truthy, so
  `proj.get(key) or 0.0` handed the NaN straight back and one unprojected
  starter turned a whole team's `starters_proj` into NaN. It is 0 now, which
  under-counts by that player rather than destroying the total.

**As-of market snapshots**
- The replay's oldest stated limit was that projections and ADP are today's, not
  as of the pick. ESPN keeps no history — no surface answers "what was his ADP
  at pick 87" (`docs/data-sources.md`, "Draft history: what ESPN keeps") — so the
  watch, the only process present while the draft runs, now records it.
  `watch.write_snapshot` files the market for the players still available after
  INIT and after every SELECTED to
  `~/.ffdraft/state/snapshots_<league>/<pick>.parquet`, keyed by the pick then on
  the clock. Columns: `_key`, `player_id`, `adp`, `espn_rank`, `espn_proj`.
- Bounded at `watch.SNAPSHOT_ROWS` (300) rows per file, cheapest ADP first: a
  full 16-team 14-round draft is 224 files and single-digit megabytes. A failed
  write is logged and dropped, never raised — the socket loop must not lose a
  pick to a snapshot, and a test asserts the pick is still announced when the
  board cannot be written.
- `replay_draft(as_of=True, snapshots=...)` and `draft_replay(league_id=...,
  as_of=true)` price each pick from its snapshot instead of today's board.
  Coverage is reported, never assumed: the `as_of` block gives picks covered
  (none exist before the watch first connected), the first and last covered
  pick, the mean share of each pool the snapshot reached, and how often the
  player actually taken was inside it. Uncovered rows keep today's numbers.
- Documented in `docs/data-sources.md` ("As-of market snapshots") with the
  column table and the bounds. Tested against `tests/fixtures/espn_draft_init.b64`
  and a fake notify; no test opens the draft socket.
- `just asof [league_id]` prints the coverage and the as-of replay against
  today's. On the live record it reports 0 of 122 picks covered and says so
  outright — the recorded draft predates this code, so there is nothing to read
  and the two runs are identical, which is the right behaviour for an as-of
  option with no snapshots. Positive control at real scale, against a
  throwaway league id with ADP shifted +10 for picks 60 onward: coverage 63 of
  122 (0.516, first 60, last 122), mean pool share 0.553 (300 rows of a ~540-row
  pool), the player taken inside the snapshot 57 times, exactly 57 picks' `reach`
  moved and every one by +10 (pick 61: 27.6 -> 37.6), pick 30 unchanged, survival
  Brier 0.128 -> 0.143. 63 files, 870 KB, so a full 224-pick draft is about 3 MB.

**Counterfactual replay**
- `draft_counterfactual` (`replay.counterfactual_draft`, `just counterfactual
  [slot] [policy] [seed]`): the replay walk with the model intervening. At each
  of one slot's turns the model picks for that team's simulated roster and the
  room's drift; the pick changes what is left downstream; every other team takes
  the walk-forward blend predictor's choice (`choice.WalkForward`, fitted
  prequentially on the real picks up to that point — `argmax` by default,
  `sample` with a seed available). Reports the roster the model would have
  built, projected starter points against the real roster, and the substitution
  at every turn. Labelled `simulation: true`.
- Three timelines run in step. The real one only fits the predictor. The model
  arm is the intervention. The **control** arm is the same simulated room with
  the target team mirroring its real picks (falling back to the predictor where
  the room has already taken one, counted as `control_picks_unavailable`), so
  `starters_proj.delta_vs_control` holds the room fixed and is the intervention
  alone, while `delta_vs_real` also carries the difference between the
  predictor's room and the real one. Without the control the only available
  number mixed the two and read as if it were the first.
- A real pick the board cannot model (kicker, defense, unprojected player) is
  mirrored rather than predicted for the *other* teams, so the simulated room
  does not eat a modelled player who really was still on the board. It is not
  mirrored at the target slot: the real pick scores 0 there whatever happens,
  and those are the turns a substitution is most likely to be worth points.
  Pool exhaustion is a separate branch with its own counter, not folded into
  the off-board one.
- Players are held by board row, not by normalised name: two rows can share a
  key (the same player listed twice, or two players with one name at different
  positions), and a name-keyed pool removes both at once. A recorded pick is
  resolved to a row by position where it has one.
- `board.lineup_value` is `team_strength`'s per-team scoring extracted, so a
  simulated roster is scored by exactly the logic that scores a recorded one;
  it now takes a pick at its word when the pick carries its own `proj_points`,
  which recorded picks never do. `choice.WalkForward.probabilities` exposes one
  predictor's distribution over an arbitrary pool without training on it.
- On the live record (122 picks, slot 4, argmax): projected starter points,
  model 1494, control 1058, real 1343 — the intervention is +436 against the
  control and +151 against the real roster. 7 of 7 turns substituted. 106 of
  111 other-team picks differ from the real draft, 4 off-board picks mirrored,
  and the control could not have 3 of its 7 real picks. Those numbers are the
  point as much as the delta is: the predictor's room is not that draft's room,
  and a control missing three of its own picks is not quite the real drafter.

**Walk-forward choice model**
- `choice.py`: four conditional-logit predictors of what the room takes
  (ESPN list order, ADP order, the model's order, and a blend with roster
  need, positional run and injury), fitted prequentially, scored out of
  sample on every pick (log loss, top-1/3/5, median rank), with a forecast
  for the pick on the clock. Reported by `draft_replay`, `predict_pick`,
  `just replay` and `just predict`.

**Team-specific effects in the choice model: measured, not adopted**
- `choice.TeamConditionalLogit`: league weights plus a per-team deviation on the
  three rank features and on new position indicators (`is_QB`/`is_RB`/`is_WR`/
  `is_TE`), fitted by ascending one penalised average log-likelihood — `L2` on
  the league weights, `TEAM_L2` (0.5, 25x stronger) on each team's deviation, so
  a deviation has to survive a much harder penalty on seven or eight picks.
  Roster need, positional run and injury stay league-wide: `need_mult` is already
  computed from that team's own roster, so a deviation on it would fit the same
  thing twice.
- Off by default (`choice.TEAM_EFFECTS = False`). `replay_draft(team_effects=
  True)` and `just teameffects [l2]` turn it on and add two predictors to the
  same walk-forward pass: `blend_team` and its control `blend_pos`, which has
  identical features and no deviations. Without that control a comparison
  against the plain blend credits the deviations with the position intercepts'
  work — which is exactly what happened on the first run here (`blend_team`
  looked 0.043 better than `blend`, and all of it was the intercepts).
- Live record, 122 picks, 117 scored out of sample, blend log loss 3.107,
  blend_pos 3.058. `blend_team` minus `blend_pos` by shrinkage: TEAM_L2 0.02
  +0.215, 0.05 +0.101, 0.2 +0.021, 0.5 +0.006, 2.0 +0.001. Worse at every level
  and monotonically approaching the no-effects model as the penalty rises: the
  best team-effects model on this record is the one with no team effects. The
  code path therefore stays off and the default blend is unchanged.
- Recorded because it is the more interesting half of the result: the *league*
  position intercepts alone (`blend_pos`) do beat the blend on log loss (3.058
  vs 3.107) and top-1 (0.197 vs 0.188) and top-5 (0.598 vs 0.564), but lose on
  top-3 (0.453 vs 0.487). Not a clean win, one draft, and out of scope for a
  change about per-team effects, so the shipped blend is left alone and the
  numbers are here for whoever picks it up.

**Predicting other teams**
- `predict_pick` (`replay.predict_pick`, `replay.team_tendency`): the model's
  choice for another team's roster, ESPN's list order, how many higher-ranked
  ESPN players that team has passed on per pick, and a prediction that
  follows whichever list the team follows. The board now carries ESPN's
  `espn_rank`.

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

- `attach_adp` joins through the alias index after the exact key ("Josh
  Palmer" / "Joshua Palmer"), alias and last-name-plus-initial hits at the
  same position only; `adp_match` records how each row joined. `draft_audit`
  reports `market_join`: unpriced rows by projection and alias-priced rows
  (`board.market_join_report`). ESPN ADP rows carry `position`.
- `names.normalize` folds accents: nflverse "Audric Estimé" and ESPN "Audric
  Estime" keyed differently, so his ESPN ADP (169.99, undrafted) never joined
  and the synthetic fallback priced him at 110.7. The walk-forward ADP
  predictor then named him the room's likeliest pick at 123. Boards carry
  `names.KEY_VERSION`; a cached board from an older normaliser is re-joined
  to its market columns on load.
- `pyproject.toml` license is an SPDX string (setuptools 77+), not the
  deprecated table plus classifier that `python -m build` warned about.
- `survival_probability_vec` no longer carries a lint suppression.

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
