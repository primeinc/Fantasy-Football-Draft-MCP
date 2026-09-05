# Tool reference

Every tool the MCP server exposes. You won't call these by name in practice — ask in
plain language and the model picks — but this is what's available and what each returns.

## Setup

### `configure_league`
Create or update a named league and make it active.

| Argument | Default | Notes |
|---|---|---|
| `name` | `"default"` | Any label. Reusing a name updates that league. |
| `teams` | 12 | Any size. |
| `draft_slot` | 6 | Your first-round pick, 1-indexed. Validated against `teams`. |
| `rounds` | 16 | |
| `scoring` | `"half_ppr"` | `ppr`, `half_ppr`, `standard`. |
| `snake` | `true` | `false` for linear drafts. |
| `qb` `rb` `wr` `te` `flex` | 1/2/2/1/1 | Starting slots. |
| `superflex` | 0 | Slots where a QB may also start. |
| `te_premium_bonus` | 0.0 | Extra points per TE reception. |
| `consistency_weight` | 0.35 | 0 = pure upside, 1 = pure floor. |
| `adp_csv_path` | — | Your platform's ADP export. Beats consensus. |

### `list_leagues` / `switch_league` / `remove_league`
Manage multiple leagues. Switching is instant — boards are cached per format.

### `prewarm`
Build every cache. **Run before draft day.** The first query otherwise pays ~8 seconds,
or minutes on a genuinely cold cache.

### `refresh_data`
Rebuild from source. `force_download=true` re-downloads everything; use when nflverse
publishes new data mid-season.

### `model_settings`
Retune factor weights: `consistency_weight`, `injury_weight`, `oline_weight`,
`schedule_weight`, `pace_weight`, `td_luck_weight`. Rebuilds the board.

`td_luck_weight` (default 0.06) controls how hard a player's red zone touchdown
rate gets pulled toward what his position converts on average — see Touchdown luck
in [methodology.md](methodology.md). Set it to 0 for a board scored on raw history
with no touchdown-luck correction.

`qb_boost` is different from the rest — those all scale a real per-player signal
(O-line, pace, etc.); `qb_boost` is a direct fractional lift on QB `draft_score`
you supply because you believe the position is worth more than the projection
says, not because of any one player's inputs. It exists because
`champion_strategies`/`draft_backtest` can show whether QB has actually beaten
its draft cost across a specific league's real history — verify that before
setting it above 0, since it isn't a universal constant. Stacks with, doesn't
replace, the roster-need discount that already stops the model wanting a second
QB once you have one — a boost makes QB1 more competitive, it doesn't undo the
one-starting-slot logic.

## During the draft

### `on_the_clock`
The whole on-the-clock workflow in one call: `sync_draft` (fresh pull, no cached
state) → `draft_status` (round, on-the-clock, roster, confirmed against the sync)
→ `who_should_i_pick` (recommendation, reasoning, survival odds) → `value_picks`
(scoped to your current round and next) → `separation_report`, appended only when
the top recommendation is a WR or TE, for that player's route efficiency and
schedule context. Takes `sync_draft`'s arguments (`platform`, `league_id`,
`draft_id`, `pasted_board`, `season`) plus `limit` for how many recommendations
`who_should_i_pick` returns. Use this instead of the five calls separately when
you're on the clock and want the full picture at once.

### `who_should_i_pick`
The main one. Returns ranked recommendations with reasoning, the pick being evaluated,
your roster, and each player's odds of surviving to your next pick.

Each row carries `espn_proj`, ESPN's full-season projection under the league's
scoring, and `espn_injury`, when ESPN ADP is configured. ESPN's projection reads
the current depth chart; the model's reads last season's box scores. When ESPN
projects under 70% of the model's number the player's role has changed (a
backup now, a new team, an injury the model cannot see) and `pick_value` is
scaled by the ratio (`model.role_multiplier`, floor 0.2); above 130% the role
has grown (a rookie or new starter the box scores lag) and it is scaled by
1.3; `why` says which. Both scalings are continuous in the ratio.

A player ESPN neither projects **nor** ranks inside `model.ROLE_UNKNOWN_RANK`
(400) is role-unknown, not neutral, and takes the same 0.2 floor: ESPN
projects everyone it treats as rosterable, so no projection and no meaningful
rank is the list saying the player has no role this season, while the model
still reads five years of box scores for him. `why` says "role unknown, value
scaled to 20%" and names the rank. A row ESPN declines to project but still
ranks inside 400 keeps 1. The multiplier divides rather than multiplies where
`pick_value` is already negative, so a discount always moves a candidate down.
`room_drift`
is the median number of picks before ADP this room has been taking players
(`replay.room_drift`), room-wide and per position once a position has 8
picks; survival odds are computed against ADP minus the per-position `shift`.
The replay's calibration is the evidence: Brier 0.140 unshifted, 0.128 with
the per-position shift, base rate 0.250, on 1060 forecasts at 122 picks.
`draft_audit` warns on every recommended player in that state and on players
ESPN does not project at all.

**Kickers and defenses** are on the board. nflverse box scores carry no kicking
and no team-defense production, so K and D/ST used to be off the board
entirely and the recommender had nothing to say in the two rounds where the
league forces you to fill both slots. They now come from ESPN's own player list
(`board.espn_special_teams`): its full-season projection under this league's
scoring is their `proj_points` — for a defense that is the yards-allowed and
points-allowed bands `league_rules` reads out of `pointsOverrides` — and
`model.score_special_teams` gives them a replacement level (the last one a team
would start, `replacement_ranks()["K"]` / `["DST"]`), a VOR and a `draft_score`
on the board's own scale. They carry the board's mean consistency, which is a
deliberate absence of a claim rather than a measurement, so `why` does not
report one for them.

They are priced on marginal value alone — the 20% share of raw `draft_score`
every other candidate keeps is a scarcity escape hatch, and neither position is
ever scarce (ESPN lists 32 of each for a league that needs one apiece). They
get their own positional need, so filling the slot registers without changing
the need of any other position. Defenses are named the way a drafted one is
recorded ("Denver Broncos D/ST", not ESPN's "Broncos D/ST"), so a drafted
defense stops showing as available and `draft_audit` no longer counts it as a
pick the board cannot resolve.

### `best_available`
Next best on the board. `sort_by`: `draft_score` (balanced), `vor`, `consistency`,
`proj_points`, or `value` (biggest ADP-to-model gap). Filter with `position`.

### `sync_draft`
Pull the live board.

- `platform="sleeper"`, `draft_id` — automatic, public API, no credentials.
- `platform="espn"`, `league_id` — public leagues work as-is; private need
  `ESPN_SWID` and `ESPN_S2` environment variables. While the draft is in progress
  the picks come from the draft room socket (cookies required, your team must be
  in the league); the browser draft room shows a "Duplicate Connection" dialog for
  a moment and reconnects. See [data sources](data-sources.md).
- `platform="paste"`, `pasted_board` — any platform. Handles numbered lists,
  "Round 3, Pick 7 — Name", comma-separated runs, trailing team and position tags.

### `watch_draft` / `stop_watch`
ESPN only. `watch_draft(league_id)` holds the draft room socket open for the team
`ESPN_SWID` owns and pushes every pick into the Claude Code session as a channel
message the moment it happens, with a recommendation once you are within three
picks of the clock. The board stays current for `who_should_i_pick` and
`draft_status` either way. Events reach Claude only when the session was started
with `claude --dangerously-load-development-channels server:fantasy-draft`
(channels are a research preview). ESPN allows one draft-room connection per
team: starting the watch closes your browser draft room with a "Duplicate
Connection" dialog, and opening the room again pauses the watch (it pushes a
"paused" event and does not fight back). Keep the room closed while the watch
runs and draft with `make_pick`, or open it to pick, then call `watch_draft` again.
One watch per league; `stop_watch(league_id)` ends it.

### `draft_room`
Who is in the ESPN draft room right now and the latest room chat, from the running
watch's socket, with team and owner names from the league member list. `upcoming`
lists the next five picks in order: pick number, slot, team and owner, whether
that team is in the room, and whether the pick is yours.

### `draft_room_stats` / `just roomstats [dump_dir]`
Who was in the draft room, for how long, and who talked — the office report.
Per member, keyed by ESPN team and labelled with the team and owner names:
`minutes_in_room` and each `session` (from, to, whether it was still open when
the log ended), `joins` / `leaves`, `in_room_at_start` (the INIT snapshot's
online flags), `messages` with `messages_by_owner` and the `last_message`,
`active_hours` and `top_hours` in the machine's local time, `first_seen` /
`last_seen` in the room, `picks`, and `clock_to_pick`.

`clock_to_pick` is the seconds from the team going on the clock to its pick
landing, measured from the `SELECTING teamId secs` line when the log has one
and otherwise from the previous `SELECTED` (`measured_by` says which produced
each number). A pick with no comparable start — the first after `INIT`, or one
after an `UNDONE` — is not timed; a gap over `roomstats.PICK_GAP_CAP_SECONDS`
(30 minutes) is a draft pause and is reported under `slowest_seconds` but kept
out of the median and mean. An autopick lands a `SELECTED` at clock expiry and
the live socket does not flag one, so read the median as time until the pick,
not time a person took.

`league_activity` counts that member's topics in the read API's
`kona_league_communication` view, which is where settings changes outside the
room show up with a date; those dates feed `active_hours` but not
`first_seen` / `last_seen`, which is why a busiest hour can fall outside the
first-to-last-seen span. The JSON carries a `definitions` block saying so.

`LEFT <team> <swid> 2` is ESPN closing a connection because that team opened
the room somewhere else. For the team whose connection produced the log it is
not a departure — `watch.py` treats it as a pause — so it is counted under
`connections_replaced` and leaves the session open; for any other team it ends
the session with `ended_by: "connection_replaced"`.

Source is the running watch for `league_id` when there is one — its `lines` are
the only timestamped record of picks that exists — otherwise the dump directory
(`dump_dir`, or the newest `espn_dump_*` under the working directory), which
reads `live/lines.jsonl`, `live/init.json`, `read_api/mTeam.json` and
`read_api/kona_league_communication.json`. A dump taken without a watch holds
the join burst only, so its presence numbers are one instant and the table says
so. `table` is the same numbers as a plain-text table for an email; `just
roomstats` prints it and writes the JSON to `room_stats.json` inside the dump.

Presence is seeded from the INIT snapshot's online flags as they were, not from
`DraftWatch.online`, which every `JOINED` and `LEFT` mutates: reading the live
dict would credit everyone still in the room with the whole draft.

The socket identifies people by SWID; SWIDs are the join key and never appear
in the output. `board.league_directory_from_mteam` names an owner the member
list does not carry `board.UNKNOWN_OWNER` ("unknown member") rather than
falling back to his SWID — that fallback used to reach `draft_room` and the
watch's pushed channel messages through `DraftWatch.team_label` too. The
directory also carries `owner_ids`, the SWIDs, so chat and activity join
exactly instead of through a display name two members could share.

### `draft_replay`
Every recorded pick replayed through the model for the team that made it, with
that team's roster and the pool as it stood. Per pick: the model's choice, the
model's rank of the real pick, `proj_gap` (model choice minus actual, projected
points), and `reach` (ADP minus pick number; positive means taken early). Per
team: matches, top-3 hits, mean rank, `proj_left_on_table`, mean reach,
`off_board` count. Overall: match and top-3 rates, the survival model's
calibration (predicted vs observed odds that the model's top candidates lasted
to that team's next pick, by probability bin) and Brier score against the base
rate, plus the biggest reaches and values. The calibration is reported with the
room's drift applied and, as `calibration_without_shift`, without it.
Projections and ADP are today's, not as of the pick. `picks` caps the per-pick
rows (0 = all).

With `as_of` (and a `league_id`) each pick is priced from the market snapshot the
watch wrote when that pick was on the clock — ESPN's ADP, PPR rank and projection
as they stood then — instead of today's. Snapshots exist only from the moment a
watch first connected and reach the top few hundred available players, so the
answer carries an `as_of` block: picks covered, the first and last covered pick,
the mean share of each pool the snapshot reached, and how often the player
actually taken was inside it. Every per-pick row also carries `as_of` (was that
pick's own player priced from the snapshot) and `as_of_pool_share`. Anything
uncovered keeps today's numbers. Format and bounds in
[data-sources.md](data-sources.md), "As-of market snapshots".

`just replay [picks]` prints the same without a server. The
answer also carries the walk-forward `predictors` score sheet, `predictor_rows`
(each predictor's rank of and probability for every real pick) and the
`forecast` for the pick on the clock; see `predict_pick`.

### `draft_counterfactual`
**A simulation, not a measurement**, and labelled as one in the answer
(`simulation: true` plus a `note`). The same walk as `draft_replay`, except that
the model intervenes: at each of `slot`'s turns (yours by default) it picks for
that team's simulated roster and the room's drift, and that pick changes what is
left for every pick after it. Every other team takes the walk-forward blend
predictor's choice among the players still available — `choice.WalkForward`,
fitted prequentially on the *real* picks up to that point, so nothing from later
in the draft leaks in. `policy` is `argmax` (the predictor's likeliest player,
deterministic) or `sample` (drawn from its distribution, with `seed`).

Three timelines run in step: the real draft (which only fits the predictor), the
**model** arm, and a **control** arm — the same simulated room with the target
team mirroring its real picks instead. That control is what makes the answer
readable. `starters_proj` carries `model`, `control`, `real`,
`delta_vs_control` and `delta_vs_real`; the first delta holds the room fixed and
is the intervention alone, the second also carries the difference between the
predictor's room and the real one, which is usually the larger term. Where the
predictor's room has already taken one of the real picks, the control falls back
to the predictor as well and the pick is counted under
`divergence.control_picks_unavailable` — read that before the delta, because the
more of them there are, the less the control is the real drafter.

Also returned: `model_roster`, `control_roster` and `real_roster`; `bench_proj`
and `open_starter_slots` for each; `substitutions`, one row per turn of that team
with the real, model and control picks side by side; and `divergence`
(other-team picks and how many differ, off-board picks mirrored, picks past an
exhausted pool). Rosters are scored by `board.lineup_value` — the same
best-lineup logic `draft_strength` uses.

Two things bound what it means. A real pick the board cannot model (a kicker, a
defense, a player with no projection) is *mirrored* rather than predicted for
the **other** teams: predicting one instead would eat a modelled player who
really was still there. At the target slot it is not mirrored — the real pick
scores 0 whatever happens, and those are the turns a substitution is most likely
to be worth points. And the whole simulation is priced with today's projections,
ADP and room drift, so it is no more as-of than `draft_replay` is.
`just counterfactual [slot] [policy] [seed]` prints the same without a server.

### `predict_pick`
For the team on the clock, or a given `slot`: `should` is the model's
recommendation for that team's roster and next pick; `espn_list` is the next
eight names in ESPN's own rank order; `tendency` is how the team has chosen so
far (for each pick, how many higher-ranked ESPN players were still available;
a median of 3 or fewer marks a team that drafts from ESPN's list) and its
position counts; `predicted` follows ESPN's list at an open starting slot for
such a team, else the model. ESPN rank is today's, not the pick's.

For the pick on the clock the answer also carries `forecast` and `predictors`
from the walk-forward choice model (`choice.py`): four conditional-logit
predictors over the available pool (ESPN list order, ADP order, the model's
order, and a blend of those with roster need, the current positional run and
injury status), each fitted on picks 1..t-1 only and scored on pick t before
learning it. `predictors` reports out-of-sample log loss, top-1/3/5 rates and
median rank per predictor; `forecast` gives each predictor's top five with
probabilities, the blend's probability by position, and the fitted weights.

Team-specific effects exist in the code and are **off** (`choice.TEAM_EFFECTS`).
`choice.TeamConditionalLogit` gives each team a deviation on the three rank
features and on position indicators, shrunk to the league weights by an L2
(`TEAM_L2`) an order of magnitude stronger than the league's. Turning it on
(`replay_draft(team_effects=True)`, `just teameffects [l2]`) adds two predictors
to the score sheet: `blend_team` and its control `blend_pos`, which has the same
features without the deviations, so the pair separates what the position
intercepts buy from what being per-team buys. On the live record the deviations
are worse out of sample at every shrinkage tried — see the numbers in
[CHANGELOG.md](../CHANGELOG.md). Seven or eight picks per team is not enough
evidence to move a weight further than the penalty pulls it back.

### `draft_strength`
Every team's draft so far ranked by projected starter points: the best lineup
its picks fill under the league's starting slots, bench projection, starting
slots still open, and pick count. Names come from the running watch when
`league_id` is given; otherwise teams are labelled by slot. Picks the board
cannot model fill their slot at 0 points.

### `league_rules`
The ESPN league's rules as ESPN states them, from the `mSettings` view: draft
type, rounds and clock, starting slots, bench and IR, position limits, every
scoring value (named for the stats the model scores, by ESPN statId for the
rest), regular-season length, playoff weeks, seeding and reseed, waiver mode,
timing and budget, trade limits, review window, veto count and deadline, lineup
lock mode, matchup and playoff tiebreakers. Kicker and D/ST items are named
from the espn-api stat map (`refs/cwendt94/espn-api`); `slot_overrides` holds
the values that apply only in one lineup slot, which is where ESPN keeps the
D/ST points-allowed and yards-allowed bands with `points` 0 at the top level.
The `byes` block adds the season's
bye topology from the nfldata schedule: teams on bye per week, the last bye
week, and any bye week that falls inside the playoffs. Nothing in it is a
default assumption.

### `draft_audit`
Invariants a recommendation depends on, checked against the live board and
draft state: cached board keys equal the current normaliser's, pick numbers are
contiguous, no player is recorded twice, your picks sit on your slot's schedule,
no drafted player is in the top recommendations. `sync_draft` reports the same
`audit` block, and the watch pushes an `audit_failed` event after any snapshot
that breaks one. Picks not on the board (kickers, defenses) are a warning, not a
failure. `market_join` lists the board rows the market join could not price
(strongest projection first, with the synthetic ADP standing in), the rows it
priced through an alias, and the rows it priced on the name alone (`key_only`)
because the market lists that player at another position. The join is the name
key *and* position first — two real players share a full name often enough that
a key-only join hands the second one the first one's price — then the name
alone when the market holds exactly one player under it, then the alias index
(`names.PlayerIndex`) for alias and last-name-plus-initial hits at the same
position; fuzzy and ambiguous hits are never joined.

### Bye weeks
`who_should_i_pick`, the watch's pushed recommendation, and `best_available` carry
`bye_week`; recommendations also carry `bye_conflicts`, the players you already
hold who share that bye. `model_settings(bye_weight=0.08)` makes the recommender
cut a candidate's pick value by 8% per same-position player on the same bye and
4% per other player. Default 0, and `bye_backtest` (2022-2025, 12 paired drafts
each, weight 0.08) found why: +5.8, +7.0, -19.2, -2.0 weekly lineup points per
season, -2.1 overall. The penalty fills more starter slots, but the player it
swaps in decides ten times more, so it stays informational like `matchup_z`.
Kickers and defenses are not modelled, so their byes are yours to check.

### `bye_backtest`
Does the bye-week stacking penalty win more weekly lineup points? For each season
and seed, one mock draft with `bye_weight` 0 and one with the given weight, same
bots and noise, scored as the best legal lineup each regular-season week on real
box scores. Season totals cannot see a bye; weekly lineups can. `improvement` is
weekly points gained per season; `empty_slots` counts starter slots nothing could
fill. Run it before trusting a nonzero `bye_weight` in a league.

### `draft_queue` / `set_draft_queue`
Your ESPN pick queue, the list autopick draws from if you miss the clock.
`set_draft_queue(league_id, "Name, Name, ...")` replaces it in that order over the
watch's socket (`DRAFT_LIST id id ...`, the room's own message for add, remove and
reorder); an empty string clears it. Returns what ESPN echoed back. `draft_queue`
reads the last echo on this connection.

### `make_pick`
ESPN only, needs a running watch and your turn. Sends `SELECT <playerId>` on the
watch's socket, exactly what the draft room sends, and waits up to ten seconds
for ESPN's `SELECTED`. Irreversible once accepted. Claude confirms the player
with you before calling it. Names resolve through the board, then the ESPN
crosswalk (kickers and unmodelled players), then team defenses by city,
nickname or "X D/ST", so rounds 13 and 14 work too.

`sync_draft` refuses while a watch is connected for the league; the watch already
keeps the state current, and a resync would rewrite the same file under it.

### `just watch <league_id>`
The same watch as a standalone process, for a slow draft that outlives a Claude
session. Logs every event to `~/.ffdraft/state/watch_<league>.log` and keeps the
pick state current so `who_should_i_pick` is right when you come back. Pauses if
you open the draft room, like the tool.

### `reload_code`
Reload the server's code from disk without a reconnect. Every `ffdraft`
module is re-imported in dependency order, `server.py` last and in place;
the tool registry the transport serves is rebuilt from the reloaded
functions (`server._sync_tools`), and `notifications/tools/list_changed` is
sent, which Claude Code honours by refreshing the tool list. The server
declares `tools.listChanged` for that. Process state survives: the running
draft watch and its socket, the ESPN queue it holds, cached boards and
settings. The watch picks up reloaded model code on its next recommendation.
A module that fails to import keeps its previous code and is named in the
result. A reconnect is still needed for a change to the server's process
environment or the `.mcp.json` registration.

### `dump_draft` / `just dump <league_id> [out_dir]`
Everything ESPN reports about the league's draft, written under
`<out_dir>/espn_dump_<league>_<season>_<stamp>/` (default `out_dir` is the
working directory; the pattern is gitignored because `mTeam` carries every
member's name and SWID). `read_api/<view>.json` is one file per read-API view
(`espn_dump.READ_VIEWS`, plus `kona_player_info` with the full player pool and
`leagueHistory`), saved as received; a non-200 view is still written and listed
under `errors` in `manifest.json`. `live/` holds the draft room's `INIT`
payload raw (`init.b64`) and decoded (`init.json`), the picks with draft slots
(`picks.json`), and `lines.jsonl`: every socket line with a receive timestamp
in ms. From a running watch that is every line since it joined, the only
timestamped record of picks that exists; `just dump` opens the room once
instead and captures the join burst only, bumping a browser room or watch.

### `record_pick` / `undo_pick` / `reset_draft` / `draft_status`
Manual board management. `record_pick` accepts shorthand.

### `plan_my_draft`
Simulate every remaining pick from your slot. `strategy`: `balanced`, `zero_rb`,
`hero_rb`, `robust_rb`. ADP-driven, so treat it as preparation rather than a script.

## Research

### `player_report`
Every modelled factor for one player: production, role, environment multipliers, injury
components, separation, draft capital for rookies. Includes red zone role
(`rz_touches`, `rz_td`, `rz_td_rate`) against the position's baseline conversion rate
(`rz_baseline_rate`) and the resulting `m_td_luck` multiplier — surfaced in the plain-
language `summary` as "touchdown regression" whenever it moves the projection.

### `compare_players`
Two to four players head to head, with a verdict.

### `rookie_report`
This year's class, projected from draft capital and landing spot. Widest error bars on
the board.

### `separation_report`
Separation, cushion, YPRR and TPRR. Pass `player_name` for one player's history, or
`position` for a leaderboard. Only players clearing 250 estimated routes and 50 targets.
Ranked by `sep_score` (talent). Also returns `matchup_z` (the player's team's upcoming
schedule difficulty at that position -- the season-long, team-level stand-in for a
WR/CB matchup chart), shown for reference only: a backtest (`matchup_backtest`) found
blending it into the ranking made WR predictions worse than talent alone, not better.

### `value_picks`
Where the model disagrees with the market. `direction`: `undervalued` or `overvalued`.
Restricted to players the market actually ranks. `adp_source` says which market:
`espn` when `ESPN_LEAGUE_ID` and the cookies are set (ESPN's own average draft
position, the list the room drafts from), otherwise `consensus`. A board cached
under consensus is repriced on the next load once ESPN ADP is configured.

### `team_context`
An NFL team's offensive environment: O-line ranks with history, pace, run/pass split,
schedule difficulty, divisional games, drive efficiency, and red zone play-calling
identity.

`drive_efficiency` (`pct_td`/`pct_fg`/`pct_punt`) is the share of that team's drives
ending in each outcome -- a multiplier on how many scoring chances its players get,
already reflected in their raw points, so read it as a confidence check on a role
rather than an extra score adjustment. `redzone_identity.shift` is neutral-field pass
rate minus red zone pass rate: a large positive shift means the offense goes notably
run-heavy inside the 20 (that team's receivers keep season-long volume but may lose
target share exactly where touchdowns happen); near zero or negative means the passing
game keeps its role in the scoring area too. Both are informational, like `matchup_z`
in `separation_report` -- not folded into `draft_score`, since blending an
unvalidated new signal into the score is exactly the mistake `matchup_backtest` exists
to catch.

Needs the `fixed_drive_result`/`drive` play-by-play columns, added after earlier cached
`play_by_play` parquets were built. If `drive_efficiency`/`redzone_identity` come back
empty for a season that should have data, run `refresh_data(force_download=true)`.

### `defense_report`
Fantasy points allowed by position, current season and five-year. Rank 1 = toughest.

### `draft_value_history`
Backtest consensus rank against actual finish. `group_by`: `draft_round` or `position`.
Converted to your scoring format.

### `persistent_value_players`
Players who beat their draft cost repeatedly rather than once.

### `matchup_backtest`
Validates whether blending schedule difficulty into talent predicts actual finish
better than talent alone. Talent comes from the *prior* season's separation score
only, schedule difficulty from the same leakage-free `strength_of_schedule` the live
recommender uses — nothing here has seen the season it's scoring. Reports Spearman
correlation and top-N precision for both metrics side by side, plus the players
where schedule swung the pick most. 2021-2024 result for WR: talent alone wins —
`separation_report` ranks by `sep_score` accordingly. Re-run this if the model
changes to see whether that still holds.

### `redzone_shift_backtest`
Backtest: does blending a team's red zone identity shift (from `team_context`) into the
existing touchdown-luck signal predict next season's points better than touchdown-luck
alone? Same leak-free discipline and same output shape as `matchup_backtest`, scored by
the same summary. A 2022-2025 run found the shift makes predictions *worse* for both WR
(`improvement_corr` -0.006, 300 player-seasons) and TE (-0.053, 117) — which is why
`redzone_identity_shift` stays informational-only in `team_context` rather than feeding
`m_td_luck`/`draft_score`. WR/TE only, since a pass-rate shift has no defensible sign for
a running back.

### `draft_backtest`
Replays a real past ESPN draft round by round: what `who_should_i_pick`'s algorithm
would have recommended given the real board at that exact moment, the true
hindsight-optimal pick by value over replacement (QB capped at 1 — a second
quarterback can't start, so it isn't ranked against real RB/WR/TE need), and what
you actually took, all scored on real points from that season. `league_id` and
`season` are all it needs — your team and draft slot are auto-detected from
`ESPN_SWID`/`ESPN_S2`, and league settings (teams, scoring, roster) are read
straight from ESPN. The board is leak-free: every history-derived input is bounded
to seasons strictly before the one being predicted, same as `matchup_backtest`.

Each of the three picks in a round also carries a value verdict (preseason ECR
against actual finish — the `value_picks` steal/bust framing, against real
outcomes instead of projections) and team context (that player's team's O-line
ranks, pace, and schedule difficulty for the season being tested — the same
numbers `team_context` reports, but leak-free for a past season instead of
always reading today's). K/DST aren't modelled anywhere in this tool, so those
rounds report your actual pick only, with no value or team context. ESPN only,
for now.

### `mock_draft`
Monte Carlo mock draft: the live algorithm against many simulated opponents,
averaged. Unlike `draft_backtest`, no real draft is needed or used — the other
teams are bots that pick by that season's real preseason ADP with realistic
reach/fall noise (bigger swings plausible late, tight consensus at the top)
rather than following it exactly, so who's actually on the board at your turn
varies draw to draw. Your slot (from your *active* configured league — run
`configure_league` first) runs the same `recommend()` logic `who_should_i_pick`
uses live, and everything is scored on real points from `season` against the
same leak-free board `draft_backtest` builds.

One draw can make the algorithm look better or worse than its true average just
from bot luck, which is why this runs `n_trials` (default 30) and reports the
mean/median/range, not a single result. For each round it also reports the most
common picks and how often each showed up — rounds with no real consensus
(usually round 6+) should be read as "plausible outcomes," not "the pick." K/DST
aren't modelled, so only skill-position rounds run (your league's total rounds
minus its K and DST starting slots).

### `champion_strategies`
What actually won your ESPN league, season by season, and which specific pick
made the difference — not just what the champion drafted, but which draft-cost
bet paid off. For each season it finds whichever team finished 1st and pulls
their real draft, running every pick through the same preseason-ECR-vs-actual-
finish value verdict `value_picks`/`draft_backtest` use. Reports each
champion's opening two picks, first QB/TE round, RB/WR volume, and biggest
steal, plus cross-season patterns: how often champions opened RB-RB, and the
median round of their first QB. ECR history only goes back to 2020 — earlier
seasons get position/timing data but no value verdicts. ESPN only.

`biggest_steal` also explains *why* it was a steal, not just that it was:
`usage_trend` is that player's real early- vs. late-season carries/targets/
target share (or pass attempts/yards for a QB) — an actual role or volume
change visible in the box scores, not assumed — and `team_environment` is his
team's O-line ranks, pace, and pass/rush split that season. Most value picks
turn out to be a volume or situation story, not raw talent beating a forecast;
this is what shows it concretely instead of leaving it to speculation.

### `resolve_names`
Check how names resolve before trusting a paste sync. Reports match type per name.

## Environment variables

| Variable | Purpose |
|---|---|
| `FFDRAFT_SEASON` | Season being drafted (default 2026) |
| `FFDRAFT_SEASONS` | Override lookback, e.g. `2021,2022,2023,2024,2025` |
| `FFDRAFT_CACHE` / `FFDRAFT_DATA` / `FFDRAFT_STATE` | Storage paths |
| `ESPN_SWID` / `ESPN_S2` | Private ESPN leagues — see [SECURITY.md](../SECURITY.md) |
