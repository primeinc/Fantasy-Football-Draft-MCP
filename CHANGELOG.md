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

**Roles: start probability, handcuffs, opportunity, role entropy**
- New `roles.py`, built from the strategy brief's objective
  (`VORP + VONA + Expected Starting Utility + Contingent Upside + Roster
  Optionality - ...`). `recommend` already priced the first two; this adds the
  next two behind weights that default to 0, and scores the role-risk term.
  `model.py` gains one kwarg, one block in the `pick_value` chain and two
  trailing lines in `explain`; everything else is in the new module.
  `just roles [what] [seasons] [trials] [seed]` runs the evidence.
- **Start probability** (`roles.start_probability`, weight `start_prob`,
  default 0). With no FLEX slot an RB3 does not compete with a WR3 for a
  starting job: he is in the lineup in a week only when fewer than two of the
  running backs ahead of him *on your roster* are available. Availability per
  week is `exp_games / 17` — the same injury mapping `project` already uses, so
  the two cannot drift — and a man on his bye is not available. The count is an
  exact Poisson-binomial over the men ahead, averaged over the 14 fantasy weeks.
  Bench value is that probability times his projection. Exact with no FLEX; a
  lower bound with one, since a flex start is not counted.
- **Contingent handcuff value** (`roles.handcuff_table`, weight `handcuff`,
  default 0). The brief's `EV = P(role change) x delta value + standalone`, with
  the two numbers kept apart rather than collapsed into "he is a good handcuff".
  The direct backup — depth rank 2 at his own NFL team and position, by the
  model's own projection — inherits the games the starter is expected to miss at
  the per-game upgrade between them; holding the starter doubles it, because the
  contingency then covers a slot the roster depends on. Added to `pick_value`,
  not multiplied: a deep bench player's `pick_value` is negative, so scaling it
  by his handcuff case pushed him further down. Restricted to depth rank 2 for
  the same class of reason: the gap to the starter is widest for the man least
  likely to inherit anything, so an unrestricted version put fourth-string backs
  and a practice-squad quarterback at the top of the list.
- **Opportunity, decomposed and named** (`roles.opportunity_shares`):
  `target_share`, `carry_share`, `redzone_share`, `snap_share`, each of the
  player's own team's total, recency-weighted the same way production is, in
  `player_report` and in `explain()`. Red zone share comes from the play rows,
  which name the team he took each touch for. A player traded mid-season is
  collapsed to one row per season against a denominator weighted by the weeks he
  spent at each club, so the trade neither double-counts the season nor discards
  the half of his production at the other one. Coverage on the live 2026 board
  (632 rows): target 552, carry 552, red zone 516, snap 526; medians 0.043,
  0.005, 0.045, 0.481. Informational only — `recommend` returns bit-identical
  names and `pick_value`s with the four columns dropped.
- **Role entropy** (`roles.role_entropy`), in `player_report` and `explain()`.
  `proj_disagreement` is |ln(ESPN / model)|, full at a factor of two (ln 2);
  `role_churn` is the week-to-week coefficient of variation of the player's
  share of his team's offensive snaps in his last season, full when its standard
  deviation equals its own mean; the score is their mean. Both scales are policy
  and quotable as a sentence about the world, not fitted — an earlier pair at 0.5
  put the median at 0.79 and separated nobody; these put it at 0.52.
  `entropy_kind` splits the brief's two uncertainties: ESPN above a model built
  from past production is `unresolved upside` (50 rows), below it is
  `role in doubt` (208), inside a quarter either way it is unnamed (374).
  Evidence: entropy binned against real projection error on a leak-free board,
  three bins, both seasons monotonic. 2024 (n 356): 0.381 / 0.529 / 0.707 mean
  absolute percentage error, spread +0.326. 2025 (n 347): 0.366 / 0.510 / 0.704,
  spread +0.338. Past seasons have no ESPN projection, so that scores the churn
  half only; the disagreement half is the same signal `role_multiplier` already
  ships evidence for. Entropy changes no number in `pick_value`.
- **Both weights ship at 0.** `roles.weight_backtest` runs the paired Monte
  Carlo `bye_backtest` already uses — same season, same seed, same bots, same
  noise, scored on real box scores as the best legal lineup each regular-season
  week, so the pair differs in exactly the weight. `just roles weights` runs it.
  It also reports `trials_improved_of_changed`: about half the trials draft the
  identical roster, and counting a tie as a loss is how a term that wins most of
  the drafts it touches reads as a coin flip.
  `start_prob` at 1.0, 12 paired drafts per season: 2024 -10.6 weekly points
  (4 of the 9 trials it changed improved), 2025 +20.6 (8 of 11), overall +5.0
  over 37 changed picks. Opposite signs by season is not evidence, so the weight
  stays 0 and start probability is reported rather than priced —
  `who_should_i_pick` carries `starts_in_a_given_week` and `bench_value` per
  candidate whatever the weight is. On the live board at pick 125 that puts
  Dalton Schultz at 0.17 and a bench value of 27.7 against a 163-point
  projection, because the roster already holds a tight end and the league has no
  FLEX slot.
  `handcuff` at 1.0, gated, 20 paired drafts per season across two seed blocks:
  2024 +17.5 (7 of 12 changed trials improved), 2025 +20.2 (9 of 11), overall
  +18.9 over 74 changed picks. All four block-seasons are positive (+37.9 and
  +3.9 in 2024, +22.1 and +18.9 in 2025), which is the only consistent sign
  either weight produced. It still stays 0, and the reason is the next entry.
- **What this backtest can and cannot resolve.** Running both weights together
  gave 2024 +18.4 on seeds 0-11 and 2024 -21.3 on seeds 8-19: the same
  configuration, the same season, two mostly-disjoint seed blocks, and a
  40-point spread with opposite signs. The gated handcuff term did the same
  thing more mildly in 2024 (+37.9 against +3.9). So the seed-to-seed spread of
  this machinery at 8-12 paired drafts is about the size of every effect
  reported above, and none of the four weight numbers is separable from noise at
  this sample size — including the handcuff term, whose consistent sign is
  suggestive and whose magnitude is not pinned down at all.
  That is a fact about the measurement, not about the features, and it is the
  reason both weights ship at 0 rather than an argument for either of them.
  What would settle the handcuff term is a run long enough for the blocks to
  agree with each other — `just roles handcuff 2024,2025 40 20` extends the
  sample rather than repeating it — and a season outside 2024-2025.
  `bye_backtest`'s -2.1 over 12 paired drafts per season was read against the
  same machinery and deserves the same caution.
- The handcuff term was **redesigned twice under its own evidence**, which is
  the reason to run it before shipping it rather than after. Version one made
  the bonus a multiplier on `pick_value` and gave contingent value to everyone
  behind the starter. Sorting the live board by the new column put Gus Edwards,
  a practice-squad quarterback and Odell Beckham at the top: the gap to the
  starter is widest for the man least likely to inherit anything. It also
  multiplied a deep bench player's negative `pick_value`, making a strong
  handcuff case push him *down*. Version two — add the points, and only for
  depth rank 2 — drafted backup quarterbacks instead, because a QB2's starter
  has the best per-game output on the board and the bonus landed after
  `need_mult` had already discounted him, walking straight past the rule that
  stops the model rostering a second quarterback. Measured over 8 paired drafts
  per season: 2024 -103.5 weekly points (2/8 trials improved, 53 players
  swapped, empty starter slots 7.5 -> 13.4), 2025 -49.5 (3/8, 44 swapped, 5.25
  -> 8.25), overall -76.5. The picks it swapped in were Cooper Rush, Gardner
  Minshew, Michael Pratt, Tyler Huntley, Clayton Tune, Trey Lance and Mitchell
  Trubisky. Version three gates the bonus by the chance the player would be in
  *my* lineup when the promotion comes: contingent points a roster can never
  start are not points. Gated, the same 8 paired drafts per season give 2024
  +37.9 and 2025 +22.1, overall +30.0 — the gate alone is worth +141 weekly
  points in 2024.
- `model.recommend` applies `roles_mult` as `pv * m` above zero and `pv / m`
  below it, so below 1 always means further down the list. `pick_value` goes
  negative deep in the board, and multiplying a negative by a start probability
  of 0.3 moves it *toward* zero — promoting exactly the bench players the
  discount exists to bury. Found by reading marge's identical fix for
  `role_mult` on the K/DST branch, after a first `start_prob` measurement had
  already been taken against the broken form and had to be thrown away. Otto
  measured the same defect a third time in `need_mult`, so it is one missing
  invariant at three sites rather than three bugs; a shared `_discount` helper
  is being proposed and this site should join it.

**Draft room presence**
- `draft_room_stats` and `just roomstats [dump_dir]` (`roomstats.py`): who was
  in the ESPN draft room, for how long, and who talked, per member by team and
  owner name. Minutes in the room and each session, joins and leaves, messages
  with the sender's name and the last one, busiest hours in local time, first
  and last seen, picks made, and the seconds each pick took from the clock
  starting — measured from the `SELECTING teamId secs` line when the log has
  one, else from the previous `SELECTED`, with `measured_by` saying which. A
  pick with no comparable start (the first after `INIT`, or one after an
  `UNDONE`) is not timed; a gap over 30 minutes is a draft pause, reported but
  kept out of the median. An autopick lands at clock expiry and the socket does
  not flag one, so the median is time until the pick, not time a person took.
  `kona_league_communication` topics add activity outside the room, dated, and
  feed the hour histogram only — a `definitions` block in the JSON says so, and
  says why a busiest hour can fall outside first/last seen.
  Source is the running watch's `lines` when there is one, else a dump
  directory; a dump taken without a watch holds the join burst only and the
  report says so. Presence is seeded from the INIT snapshot's flags
  (`DraftWatch.online_at_init`, new), not from `DraftWatch.online`, which every
  JOINED and LEFT mutates: the live dict would credit everyone still in the room
  with the whole draft. `LEFT <team> <swid> 2` for the team whose connection
  produced the log is a duplicate connection, not a departure — the same reading
  `watch.py` already takes — and is counted under `connections_replaced`.
- The socket names people by SWID: it is the join key and nothing else, so no
  SWID reaches the JSON or the table. `board.league_directory_from_mteam` and
  `board.mteam_member_names` split out of `espn_league_directory` (so a saved
  `read_api/mTeam.json` reads the same way as the live view) and an owner the
  member list does not name now reads `board.UNKNOWN_OWNER` instead of his
  SWID. The old fallback put a raw SWID in `draft_room` and in the watch's
  pushed channel messages through `DraftWatch.team_label`, not only in this
  report. The directory also carries `owner_ids` so chat and activity join on
  the SWID rather than on a display name two members could share.

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

**Hot reload**
- `reload_code`: re-imports every `ffdraft` module, rebuilds the served tool
  registry from the new functions and sends `notifications/tools/list_changed`;
  the server declares `tools.listChanged`. Watches, sockets, queues, boards
  and settings survive (module globals are guarded with `globals().get` so a
  re-execution keeps them). Ends the reconnect-drops-the-watch cycle for code
  changes.
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
- Verified as a pure refactor on the live 632-row board: `just replay` returned
  identical numbers before and after (122 picks scored, 117 on board, Brier
  0.128, log loss 0.412, blend 3.107 / top1 0.188). The two keyings are
  identical whenever every key is unique, and on the current 696-row board one
  key is on two rows — Gabe Davis, twice, as the same WR/BUF row with the same
  projection — so they remain indistinguishable there too. Re-run the check
  after integration: the baseline moves with any change to the survival
  distribution or the pick_value ordering, and a moved baseline is not a changed
  refactor.
- A recorded pick that carries no position and whose name is on two board rows
  is resolved to the first untaken one, which is a guess. The replay reports the
  picks it guessed on (`ambiguous_name_picks`), and the counterfactual counts
  them in `divergence.ambiguous_name_rows`, rather than the walk quietly
  pretending it knew. Normally empty.
- `lineup_value` requires a pick that carries `proj_points` to carry `position`
  too — both or neither. Falling back to the name for the position would
  reintroduce exactly the ambiguity the projection is passed to close.
- `board.lineup_value` counted a NaN projection as NaN, not 0: NaN is truthy, so
  `proj.get(key) or 0.0` handed the NaN straight back and one unprojected
  starter turned a whole team's `starters_proj` into NaN. It is 0 now, which
  under-counts by that player rather than destroying the total.

**As-of market snapshots**
- The replay's oldest stated limit was that projections and ADP are today's, not
  as of the pick. ESPN keeps no history — no surface answers "what was his ADP
  at pick 87" (`docs/data-sources.md`, "Draft history: what ESPN keeps") — so the
  watch, the only process present while the draft runs, now records it.
  `watch.write_snapshot` files the market for the players still available to
  `~/.ffdraft/state/snapshots_<league>/<pick>.parquet`, keyed by the pick then on
  the clock. Columns: `_key`, `player_id`, `adp`, `espn_rank`, `espn_proj`.
- Written after INIT (the seed), after each SELECTED, and again on SELECTING —
  ESPN naming the team that has just gone on the clock, which is the event this
  wants rather than a state inferred after a different one, and which arrives
  again when the clock reopens so the rewrite self-corrects. UNDONE deletes the
  files for the rolled-back picks (`watch.drop_snapshots_above`): a snapshot of
  a board state the draft backed out of would let a later replay price a pick
  from a world that did not happen.
- Each write re-reads the board through the watch's `refresh` callback.
  `DraftWatch.board` is otherwise the board the watch was *constructed* with —
  only `_recommendation` refreshed it, on the handful of picks near your own
  turn — so without this every file held identical ADP while the coverage block
  reported success: a silent failure that looks exactly like the feature
  working. Caught in review by lena; the regression test refreshes the board
  between two snapshots and asserts the two parquets differ.
- Bounded at `watch.SNAPSHOT_ROWS` (300) rows per file, cheapest ADP first: a
  full 16-team 14-round draft is 224 files and single-digit megabytes. A failed
  write is logged and dropped, never raised — the socket loop must not lose a
  pick to a snapshot — but not silent: the first failure pushes a
  `snapshot_failed` channel event and then it stays quiet until a write
  succeeds. `draft_room` and `stop_watch` report `as_of_snapshots` next to
  `picks_seen`, so "picks_seen 122, as_of_snapshots 0" is one line to read.
- `replay_draft(as_of=True, snapshots=...)` and `draft_replay(league_id=...,
  as_of=true)` price each pick from its snapshot instead of today's board.
  Coverage is reported, never assumed: the `as_of` block gives picks covered
  (none exist before the watch first connected), the first and last covered
  pick, the mean share of each pool the snapshot reached, and how often the
  player actually taken was inside it, and every per-pick row carries `as_of`
  and `as_of_pool_share` of its own. Uncovered rows keep today's numbers. The
  block reports the snapshot directory's basename, not its absolute path, which
  on a home directory carries the user's account name.
- `watch.resolve_snapshots` treats a string that names a directory as a
  directory rather than as a league id. `draft_replay` takes the argument over
  MCP, where every value is a string, so a caller passing a path would otherwise
  have read `STATE_DIR/snapshots_<the whole path>` and found nothing — reported
  as "no snapshot" rather than as an error.
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

**ESPN's "undrafted" ADP is a placeholder, not a pick number**

- `averageDraftPosition` is not null for a player ESPN's population never
  drafts: it is filled with a value near ESPN's default draft length. On the
  2026 list 823 of 999 rows land in one 4-pick-wide bin — 260 share exactly
  169.99 and 208 share exactly 170.00. A value 468 players share cannot be a
  draft position, since only one player is taken at each pick.
- Read as a pick number it told the model that most of the board was about to
  vanish. `plan_my_draft`'s availability filter (`adp > pick - 1.1*sqrt(pick)`)
  treated every such row as already gone once the pick number passed ~174: the
  candidate pool went from 550 at pick 164 to **nine** at pick 189, and *not one
  of the nine had an ESPN projection at all*. That is why the plan's last three
  picks were Jakobie Keeney-James, Drake Dabney and Gage Larvadain — men with no
  NFL role — and it is not something the role-unknown scaling could fix, because
  there was nothing else left in the pool to prefer.
- `board.undrafted_adp_mask` finds the placeholder instead of hardcoding it,
  because its value follows ESPN's default draft length: the most-repeated ADP,
  accepted only when more than `UNDRAFTED_MIN_TIES` (20) players share it, plus
  a `UNDRAFTED_ADP_TOLERANCE` (1.0 pick) run either side for the smear the
  averaging leaves across neighbouring hundredths.
  That tolerance is load-bearing, not decorative, and otto was right to ask:
  468 rows sit exactly on 169.99 or 170.00 and another 326 are caught only by
  the run. Whether those 326 belong turns on whether their ADP carries signal,
  and it does not. Outside the band, ADP and ESPN's own rank correlate at
  rho = +0.95 and mean ADP rises 47.7 -> 127.8 -> 159.3 -> 168.6 across rank
  buckets. Inside it rho = +0.09, the whole spread is 0.18 picks against 53.7
  outside, and mean ADP across rank buckets 199-400, 400-900, 900-1500 and
  1500-2500 is 170.18, 169.93, 169.98, 169.99 — not even monotone. In the
  tolerance-only subset rho = **-0.29**: a better-ranked player has a *later*
  ADP, which is the opposite of a draft position. Shrinking the tolerance to
  absorb float noise alone would leave 326 rows carrying a number that runs
  backwards against rank.
  Ownership is untouched and is a separate question: some of the 326 are real
  players rostered in 15-34% of leagues (Cairo Santos, Tre Tucker, Pat
  Freiermuth) against a maximum of 0.51% among rows sitting exactly on the
  placeholder. The only claim made about them is that ESPN's ADP does not price
  them, which the correlations show of every row in the band.
  A market frame with continuous ADPs (a pasted CSV, consensus ECR) trips
  neither condition and is untouched.
- Those rows go to the same synthetic fallback that already covers a row the
  market join missed, so every consumer of `adp` — survival odds, the
  availability filter, the reach numbers — reads "no market price" rather than
  "about to go at 170". They keep `espn_proj` and `espn_rank`, since losing
  those would have made all 449 of them look role-unknown. A new `adp_source`
  value `undrafted` keeps the two causes apart, and `market_join_report` counts
  it separately: 449 undrafted against 9 genuinely unjoined.
- Live board at 122 picks, one-step before -> after. `adp_source` espn 687 ->
  espn 238 / undrafted 449, modelled 9 either way. The plan's pool at pick 189
  9 -> 441, of which those carrying an ESPN projection go 0 -> 275; at 196
  8 -> 439 (0 -> 274); at 221 7 -> 427 (0 -> 264). The plan itself at picks
  164/189/196/221: Detroit Lions D/ST, Jakobie Keeney-James, Drake Dabney, Gage
  Larvadain -> Zach Charbonnet, Evan Engram, James Conner, Tre Tucker. Every
  name in the new plan is a player ESPN projects; three of the four it replaced
  were not.
- The pool at pick 164 goes 550 -> 541, the only place the count falls. Those
  nine are rows the model ranks highly and ESPN does not price at all, so the
  synthetic curve gives them an early pseudo-ADP and the filter now reads them
  as already gone. That is the synthetic fallback's existing behaviour, applied
  to more rows; it is not new logic.
- Known limitation, still true and stated rather than hidden: the plan ends with
  no kicker and no defense although the league starts both. With no next pick,
  `expected_best_at_next_pick` values waiting at the *worst* player left at that
  position, so a position with a long bad tail (265 receivers down to a
  draft_score of -145) shows a far larger marginal value than one with a short
  tail (32 defenses down to -25). That is tail length, not opportunity cost, and
  fixing it is a third change to the pick-value model that none of these entries
  covers. Left undone deliberately.

**The survival model's right tail**

- `survival_probability` treated a player's realised draft slot as normal around
  his ADP. A Gaussian right tail says a player three and a half standard
  deviations past his ADP is certainly gone, and real boards are full of players
  who are not: the live record had the second-best defense undrafted at pick 123
  with an ADP of 93, in a room that had taken one defense in 122 picks, and the
  model put his survival at 0.00. That is worse than an over-urgent single
  number. When a whole position reads 0.00,
  `expected_best_at_next_pick` accumulates nothing, so waiting at that position
  is valued at its *worst* remaining player — and `marginal_value`, which 80% of
  `pick_value` is built from, is computed against that.
- The distribution is now logistic with the same spread (`model.SURVIVAL_TAIL`),
  so only the tail changes. Conditional survival well past the ADP tends to a
  constant hazard per pick instead of a cliff: "he has slid this far already, so
  the chance he goes in the next seven picks is about what it was for the last
  seven" is the right statement about a player the market has stopped pricing at
  his ADP. Computed in logs (`np.logaddexp`), so the far tail no longer needs the
  `p_gone_now >= 0.999` hard zero that used to stand in for it.
- Two numerical repairs found on the way, both in the normal path: `1 - Phi(z)`
  is catastrophic cancellation that underflows to exactly 0 past about z = 8,
  which made the numerator and denominator of the conditional probability equal
  and returned a survival of 1.0 for a hopelessly gone player. It uses `erfc`
  now. And a row with no ADP no longer reaches the tail functions at all.
- Decided on evidence, three arms over the recorded draft at 122 picks, same
  board and same picks, only the distribution varying. `shipped` is the exact
  pre-change implementation, `normal` is the repaired normal without the hard
  zero, `logistic` is what ships:
  shipped and normal are identical on every reported figure (Brier 0.129, log
  loss 0.416, and the same per-round and per-position tables), so the numerical
  repairs change nothing measurable on this record and the whole difference
  below is the tail shape. That is now counted rather than inferred, at otto's
  suggestion: of 78,159 survival evaluations in the replay, the shipped
  `p_gone_now >= 0.999` hard zero fires 27 times (0.03%) and `z > 8`, where
  `1 - Phi(z)` underflows, fires **zero** times. So the `erfc` repair is
  unexercised on this record — proven, not assumed — and the hard zero fires
  too rarely to move an aggregate. Both would be reached far more often on a
  full 224-pick draft or a deeper board, which is the case for repairing them
  regardless of what this record shows.
  logistic: Brier 0.129 -> 0.127, log loss 0.416 -> 0.401, against a base rate
  of 0.250. Log loss improves in five of seven rounds, and for QB
  (0.836 -> 0.760), WR (0.289 -> 0.275) and K (0.187 -> 0.170); RB (0.473) and
  TE (0.361) are unchanged; DST is worse (0.568 -> 0.618) on 17 forecasts. The
  `espn_list` (3.358) and `adp` (3.333) predictors are bit-identical, which is
  the control.
  **Those aggregates are not a demonstrated improvement, and the change does not
  rest on them.** This is one draft. Treating the seven rounds as blocks, the
  per-round log-loss deltas are -0.080, -0.014, -0.001, +0.014, -0.019, +0.007,
  -0.014: mean -0.015, and t = -1.3 on 6 degrees of freedom. Brier gives
  t = -1.6. Neither is distinguishable from zero, and dropping round 1 — which
  contributes more than half the total — leaves t = -0.8. Five of seven rounds
  and four of six positions move the right way, which is direction, not
  significance.
  What the change actually rests on is that the old answers were wrong
  independently of any score: a defense demonstrably on the board at pick 123
  was assigned survival 0.00, and past z = 8 the arithmetic returned 1.0 for a
  player who was certainly gone. A model that says 0.00 about something that is
  visibly true is worth replacing whether or not one draft's Brier can prove it.
  The supporting evidence is the mechanism (constant hazard rather than a cliff)
  and the lowest-probability bucket, where the normal predicted 0.040 against
  0.080 observed and the logistic predicts 0.050 against 0.070 — half the
  calibration error, on 329 and 334 forecasts, in the exact region the change
  targets. That bucket is also a single draft.
  Read the per-position rows with more care still: the replay re-derives its
  recommendations from the survival numbers, so the two runs do not score
  identical forecast sets (DST n 17 vs 18, K 9 vs 11) and the small positions
  are not paired samples at all.
  Prompted by lena finding that `roles.weight_backtest`'s seed-to-seed spread at
  8-12 paired drafts is about the size of every effect measured with it. None of
  the numbers in these entries come from that harness — every one is a
  deterministic re-run over one recorded draft or one board, with no seeds — but
  "deterministic" is not "well evidenced", and the correction applies.
- The K/DST pricing below was re-measured on the fixed tail, because the two
  interacted: the thin tail was inflating D/ST marginal value at the same time
  the carve-out was deflating its raw-value share. It still earns its keep. With
  the 0.20 raw share kept, the top defense is the second-best pick at 125 —
  round 8 of 14 — and three defenses crowd the top six; priced on marginal value
  alone it is fifth at 125 and 189, third at 164 and 196. The top defense's
  survival to the next pick now reads 0.54 at pick 157 and 0.54 at 189, against
  0.30 and 0.18 before.

**Kickers and defenses are priced, not guessed**

- K and D/ST are on the board. nflverse box scores carry no kicking and no team
  defense production, so neither position ever reached it: the recommender saw
  them as `off_board` and said nothing in the two rounds where the league forces
  you to fill both slots. `board.espn_special_teams` builds their rows from
  ESPN's own player list — its full-season projection under this league's
  scoring is their `proj_points`, which for a defense is the yards-allowed and
  points-allowed bands `league_rules` already reads out of `pointsOverrides` —
  and `model.score_special_teams` puts them on the board's own `draft_score`
  scale. Live board at 122 picks: 632 rows -> 696 (32 defenses, 32 kickers).
- `LeagueSettings.replacement_ranks()` now covers K and DST. It did not, which
  is why they had no VOR and no `draft_score`. No bench pad: nobody rosters a
  second one, so the replacement is the last one a team would start, the
  (teams x slots)th best. In this 16-team league that puts D/ST replacement at
  83.1 and K at 141.8, so the top defense is worth 47.6 points over replacement
  and the top kicker 29.6 — the gap that makes a defense worth a pick and a
  kicker much less so, now priced instead of asserted. The modelled positions'
  entries are unchanged.
- They carry the board's *mean* consistency, so `draft_score` is
  (1 - consistency_weight) x VOR: exactly what any average-consistency player on
  the board already gets. There is no week-to-week history for a kicker or a
  defense in this codebase and inventing a consistency for them would have been
  the only made-up number in the change. `explain` does not report it for them.
- `recommend` prices them on marginal value alone. The 20% share of raw
  `draft_score` every other candidate keeps is a scarcity escape hatch, and
  neither position is ever scarce — ESPN lists 32 of each for a league that
  needs one apiece — so keeping it priced the top defense as if passing on it
  cost you the slot. Measured: with the raw share, the Houston Texans D/ST came
  second at pick 125 (round 8 of 14, pick_value 8.83 against Jakobi Meyers'
  9.58) and first at 164, 189 and 196. On marginal value alone it is fourth at
  125 (2.23) and second from 132 on, which is where the league actually forces
  the slot.
- Their own positional need (1.18 for an open slot, 0.02 once filled), so
  filling the slot registers without touching the need of any other position:
  `FANTASY_POSITIONS` is unchanged and the new `SPECIAL_POSITIONS` carries K
  and DST separately, so nothing that walks the modelled positions — aging
  curves, red zone role, separation, `project()`'s replacement baselines — has
  to special-case them.
- Defenses are named the way `_espn_player_name` records a drafted one ("Denver
  Broncos D/ST"; ESPN's list says "Broncos D/ST"), or the board and the draft
  state would key the same defense differently and a drafted defense would keep
  reading as available. Live: `draft_audit`'s unresolved picks went 5 -> 3, with
  Brandon Aubrey and Denver Broncos D/ST now resolving on the board.
- Evidence, live board at 122 picks, recommendation at each of the seven picks
  left (pick_value). Before, no K or D/ST existed at any pick. After: pick 125
  Meyers 9.58, Marks 2.96, Deebo 2.95, **Texans D/ST 2.23**, Schultz 2.08; pick
  157 Meyers 11.24, **Texans D/ST 4.87**, Deebo 4.61, Marks 3.06; pick 189
  Meyers 12.52, **Texans D/ST 7.04**, Deebo 5.89, **Rams D/ST 3.49**, Marks
  3.42, **Cameron Dicker K 2.89**. `plan_my_draft` takes the Detroit Lions D/ST
  at 164 where it previously took Dalton Schultz. No backtest is possible here:
  the mock-draft loop scores weekly best lineups from box scores, which do not
  exist for kickers or defenses.
- Known limitation: the plan still takes no kicker. Its availability filter
  drops the pool to a handful of synthetic-ADP rows by pick 189 (the same
  sentinel-ADP defect recorded under the role-unknown entry below), and most
  kickers sit at ESPN's undrafted default of ~170 and are filtered out with
  everyone else.
- `explain` printed "ESPN status nan" on every defense: NaN is truthy and ESPN
  files no injury status for a team defense.
- `board.load_espn_adp` carries `pro_team_id`, and `board._ESPN_TEAM_ABBR` maps
  it to the abbreviation the board and the nfldata schedule use, so a kicker or
  a defense gets a real team and therefore a real bye week.
- The appended rows carry `is_rookie` and `off_roster` as False rather than
  leaving them empty, and `_add_special_teams` restores every boolean flag the
  board had after the concat. Left empty they widen the column to object, and
  pandas refuses to mask with an object column holding None — `b[b["is_rookie"]]`
  raised `Cannot mask with non-boolean array containing NA / NaN values` for
  every caller on the board, not just for kickers.
- `MARKET_JOIN_VERSION` 4: a cached board built before this has no K or D/ST
  rows (or has them without the boolean flags) and reprices on load.
- `who_should_i_pick` returned invalid JSON as soon as a defense reached the
  list. It emitted `r.get("espn_injury")` straight into `json.dumps`, ESPN files
  no injury status for a team defense, and NaN is truthy so no `or`-guard
  catches it. Python writes NaN as a bare `NaN` literal, which its own parser
  reads back happily and every conforming client rejects — so the failure was
  invisible from inside the process and total from outside it. Found by lena
  against my board. New `server._jsonable` walks a hand-built payload and turns
  every non-finite float, NaT and NA into null; `_rows` already did this for
  table output and the hand-built dicts had nothing. Swept the other eight
  tools: only this one leaked.

- The cost, measured and recorded rather than left for someone to find. Putting
  64 rarely-drafted players into the pool makes the walk-forward choice model
  worse: `just replay`'s blend log loss goes 3.103 -> 3.183 and its forecast for
  pick 123 goes from WR 72% / RB 22% to WR 52% / DST 17% / K 14%, in a room that
  had taken one defense and one kicker in 122 picks.
  The cause is not the pick_value ordering, and it is worth being exact because
  the obvious explanation is wrong. `espn_list` (3.315 -> 3.358) and `adp`
  (3.359 -> 3.484) degrade too, and neither reads `pick_value` at all — `adp`
  degrades most of the three. Forcing an open K/DST slot to a neutral need of
  1.00 instead of the usual 1.18 open-slot premium recovers 0.008 of the 0.080.
  Two mechanisms are left. A conditional logit over the available pool must
  spread mass across 64 more candidates, and K/DST carry real ADPs and ESPN
  ranks that place them mid-pack rather than last. And `picks_scored` goes
  117 -> 119: Brandon Aubrey and the Denver Broncos D/ST are picks the board
  previously could not model and the replay silently skipped. If the other 117
  were unchanged those two alone would carry a mean log loss of 4.88 against a
  base of 3.103, which is the whole difference. Some of the apparent regression
  is the replay no longer ducking the two hardest picks in the record.
  Left alone deliberately. Excluding K/DST from the choice model's pool would
  restore the old numbers by restoring the old blind spot, and a neutral K/DST
  need is a second position-specific constant that buys 0.008 — inside the noise
  of a single draft, by the standard applied to the survival tail above.

**ESPN projections as a role check**
- `load_espn_adp` also carries `espn_proj` (ESPN's season projection under the
  league's scoring: stats entry statSourceId 1, scoringPeriodId 0) and
  `espn_injury`; `attach_adp` puts both on the board. `model.role_multiplier`
  scales `pick_value` by ESPN/model when ESPN projects under 70% of the model
  (floor 0.2); `explain` reports the ESPN number and the scaling; `audit_state`
  warns on recommended players in that state or with no ESPN projection.
  Found live: the model had Tyrone Tracy Jr. RB22 at 185 points from 2025
  box scores while ESPN projected 41, a backup on the 2026 depth chart.
- The role check now covers the players ESPN says nothing about. A row with no
  `espn_proj` **and** no `espn_rank` inside `model.ROLE_UNKNOWN_RANK` (400) is
  role-unknown rather than neutral and takes `ROLE_FLOOR` (0.2) — not a new
  number, but what the continuous scale already gives a player ESPN projects at
  zero, since a ratio of 0 clips to the floor. ESPN publishes a projection for
  everyone it treats as rosterable, so no projection plus no meaningful rank is
  the list saying the player has no 2026 role while the model still reads five
  years of box scores for him. A row ESPN declines to project but still ranks
  inside 400 keeps 1. `explain()` says "role unknown, value scaled to 20%" and
  names the rank; `draft_audit` splits its old "no ESPN projection for" warning
  into the scaled and the unscaled case.
  Live board at 122 picks: 169 of 632 rows have no ESPN projection, and every
  one of them is unranked (9) or ranked past 400 (160 — best is Jam Miller at
  456, then Anthony Richardson at 478, and all the rest past 1300), so no row
  is left in the middle. They include Tyreek Hill, Brandon Aiyuk, DeAndre
  Hopkins, Joe Mixon and Nick Chubb, all still carrying three-figure
  projections off old box scores. `who_should_i_pick`'s top five changes at
  three of the seven remaining picks: Jared Wayne (WR, model 178.8, no ESPN
  projection, ESPN rank 1401) was 5th at pick 164 (pick_value 4.00), 4th at 196
  (7.41) and 4th at 221 (108.25); he is now scaled to 0.20 and out of all
  three, replaced by Dalton Schultz (2.63), Jayden Reed (5.39) and Josh Downs
  (105.42). Picks 125/132/157/189 are unchanged — no role-unknown player was in
  those top fives. `plan_my_draft` changes at 164 (Jared Wayne -> Dalton
  Schultz).
- Every pick_value multiplier goes through `model._discount`, which multiplies
  a non-negative value and divides a negative one, so below 1 always means
  further down the list and above 1 always means further up. Plain
  multiplication does not: almost every candidate on a live board has a
  negative pick_value (564 of 577 available rows at pick 123 of the recorded
  draft) because most players are worth less than what waiting returns, and
  multiplying a negative number by a discount moves it *toward* zero. The
  ordering inside the negative half came out inverted — the more a player was
  discounted, the higher he ranked.
  Found first on `role_mult`, where it was fixed alone; otto's review caught
  that `need_mult` had the same defect, is always on, and does far more damage,
  since `BACKUP_DECAY["QB"]` is 0.04. `bye_mult` had it too — inert only
  because `bye` defaults to 0. All three now go through the one helper, so they
  cannot drift apart on this again.
  A/B on the live board at pick 123, same board both arms, only the sign rule
  varying. Recommendation ranks 14 to 20 before were Justin Fields
  (marginal_value -33.9), Hunter Henry (-7.8), Shane Buechele (-50.4), J.J.
  McCarthy (-52.1), Daniel Jones (-20.9), Tua Tagovailoa (-27.6) and Carson
  Wentz (-68.8): six backup quarterbacks, ordered by how bad they were, sitting
  above candidates several times better. After, those ranks are Evan Engram,
  four kickers and three defenses, ordered by marginal value, and the backups
  fall to 221, 266, 320, 329 and 365 of 577. C.J. Stroud, a starter the model
  rates, holds rank 8 in both. The first eleven are identical, because a
  positive pick_value multiplies either way; the entire difference is in the
  negative half, which is 98.1% of the board.
  This reaches past the top of `who_should_i_pick`: `choice.py`'s
  `log_model_rank` feature ranks the whole pool by pick_value, and
  `plan_my_draft`, `mock_draft` and `draft_counterfactual` read the full
  ordering. `just replay`, 119 picks scored out of sample, before -> after:
  the `model` predictor's log loss 4.690 -> 4.324, its top-1/3/5 and median
  rank unchanged; the `blend` 3.189 -> 3.192 with top-1 0.210 -> 0.193 and
  top-5 0.555 -> 0.580. Stated plainly: the predictor that reads the model's
  order directly gets materially better calibrated, and the blend moves within
  noise on 119 picks rather than improving. `espn_list` and `adp` are
  bit-identical (3.358 and 3.333), as are the survival Brier (0.129) and log
  loss (0.416), which is the control — nothing that does not read pick_value
  moved. The case for the change is that the ordering was wrong, not that the
  blend got better.
  The first version of this divided, which expresses the same ordering but is
  unbounded: `need_mult` bottoms out at 0.02, so a negative pick_value could be
  inflated fiftyfold. Invisible in a ranking and ruinous in a sum — `replay`
  sums `pick_regret` per team and `_team_totals` *sorts the team table on it*,
  and one backup quarterback at pick 108 (Mac Jones, need 0.04) gave slot 12 a
  summed regret of 8641 against 398 for the next worst, with `biggest_regrets`
  showing that single row at 8494 instead of five informative ones. otto caught
  it.
  A multiplier of `m` is now read as "move this by (1 - m) of its own size" —
  what multiplying already means above zero, applied by reflection below it as
  `v * (2 - m)`. Bounded at twice the magnitude, so no single pick can take over
  an aggregate. On the live board the worst team's summed regret is 400 against
  359 for the next worst (1.1x, against 21x under division), and
  `biggest_regrets` reads Mac Jones 254, KC Concepcion 152, Rashod Bateman 110,
  Jadarian Price 96, Matthew Golden 88.
  The ordering fix survives the bound: at pick 123 the backup quarterbacks that
  multiplication put at ranks 15, 18, 19 and 20 (Justin Fields, Daniel Jones,
  Tua Tagovailoa, Carson Wentz) sit at 141, 69, 108 and 295 of 577, and C.J.
  Stroud — a starter the model rates — holds rank 8 under both. `DISCOUNT_CEILING`
  guards the one place the reflection stops being monotone; nothing in
  `recommend` approaches it (role caps at 1.3, need at 1.18).
- Known limitation this exposed, not fixed here: `plan_my_draft`'s availability
  filter drops the pool to 9 rows by pick 189, all of them role-unknown, so its
  last three picks are forced rather than chosen and the scaling cannot change
  them. 448 of the 515 undrafted board rows carry ESPN's undrafted-default ADP
  of ~170, and `adp > pick - 1.1*sqrt(pick)` treats every one of them as
  already gone once the pick number passes ~174. The sentinel ADP is the
  problem, not the filter.
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
- The exact pass of that join is position-aware (`board._exact_market_join`).
  It merged on the name key alone, so two real players sharing a full name at
  different positions collided and the second was priced as the first — the
  ESPN list carries a Josh Allen at QB and another at LB under one key, which
  is the same collision `names.PlayerIndex` already avoids for free-text
  lookups. Now it merges on (key, position), falls back to the key alone only
  when the market holds exactly one player under that name (recorded as
  `key_only`, and reported), and keeps the alias second pass. A market frame
  with no position column at all (a pasted CSV) joins on the key as before.
  On the live board at 122 picks, repricing 632 rows: before `exact` 620,
  `alias` 2, `lastname_initial` 1, unpriced 9; after `exact` 618, `key_only` 2,
  `alias` 2, `lastname_initial` 1, unpriced 9. Nothing lost its price; the two
  `key_only` rows (Riley Nowakowski, Max Bredeson — tight ends ESPN lists at
  RB) are now labelled honestly instead of counted as exact.
- `board.MARKET_JOIN_VERSION`, stamped on every board by `attach_adp` and
  checked by `server._build_board`, alongside `names.KEY_VERSION`. Without it
  the join fix above would never have reached a board already on disk: a board
  cached by the old join carries `adp_match` and ESPN-sourced rows, so every
  other clause in the cache gate passes and the stale prices survive until
  something unrelated forces a reprice. Bump it whenever `attach_adp` changes
  what a row joins to. `market_join_report` now caps `alias_joined` and
  `key_only` at `limit` like `unjoined`, and reports the untruncated totals.
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
