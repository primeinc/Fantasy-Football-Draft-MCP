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

**Walk-forward choice model**
- `choice.py`: four conditional-logit predictors of what the room takes
  (ESPN list order, ADP order, the model's order, and a blend with roster
  need, positional run and injury), fitted prequentially, scored out of
  sample on every pick (log loss, top-1/3/5, median rank), with a forecast
  for the pick on the clock. Reported by `draft_replay`, `predict_pick`,
  `just replay` and `just predict`.

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
  averaging leaves across neighbouring hundredths. That width is policy, not
  fitted, and is stated as such: it takes the spike (794 of 999 rows, median
  0.03% rostered) and leaves the nearest genuinely-drafted players outside it —
  Dalton Schultz at 168.87 and 18.4% owned, Calvin Ridley at 168.94 and 25.1%.
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
  below is the tail shape.
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
  One consequence to know: dividing inflates the magnitude of an already
  negative pick_value (Cam Ward reads -892 where he read -1.43), so `replay`'s
  `pick_regret` against a deeply negative actual pick is now a much larger
  number. Ordering is what pick_value is for and it is now right; magnitudes on
  the negative side are not comparable with earlier runs.
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
