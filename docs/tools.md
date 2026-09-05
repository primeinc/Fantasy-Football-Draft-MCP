# Tool reference

Every tool the MCP server exposes. You won't call these by name in practice — ask in
plain language and the model picks — but this is what's available and what each returns.

## How big an answer may be

A tool result over the client's limit is not shown truncated — it is not shown at
all, so an answer that overruns is unreadable rather than merely verbose.
`stream_kdst(week=1)` returned **69,512 characters** and the user could not read
the tool (`#52`).

Every payload leaves through one exit, `server._emit`, which caps it at
`PAYLOAD_LIMIT` (20,000 characters) by trimming the longest tables first and
adding a `truncated` field naming each path and how much of it survived. That is
the backstop, not the plan: each tool shapes its own answer to a readable size,
and the cap exists because no amount of shaping can promise a bound when the data
behind it can always grow.

Measured on the live league (`just payloads`), against a 20,000-character cap:

| call | characters |
|---|---|
| `stream_kdst(week=1)` | 14,151 |
| `stream_kdst(week=1, detail=true)` | 18,852 — trimmed by the backstop |
| `stream_kdst(week=1, detail=true, look_ahead=1)` | 11,839 |
| `evaluate_trade`, one for one | 5,999 |
| `evaluate_trade`, two for two | 6,053 |
| `draft_retrospective(around=0)` | 6,057 |
| `draft_retrospective(around=2)` | 9,864 |
| `draft_retrospective(around=4)` | 13,336 |

`just payloads` re-measures all of them and prints the size of each top-level key,
so a regression names the section that grew rather than only the total. The two
trade rows are measured with stand-in rows added for roster players the board
cannot price; without them `evaluate_trade` refuses outright on this league (see
its section below).

Indentation is about a third of every figure above — the payloads are emitted at
`indent=2` for readability, and dropping it is the headroom of last resort.

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

**Role weights.** `model.recommend` takes `role_weights`, a mapping opening the
two `roles.py` terms that can move a `pick_value`. Both default to 0, and at 0
the recommendation is bit-identical to one computed without them.

`start_prob` prices what a bench player is actually worth in a league with no
FLEX slot. An RB3 does not compete with a WR3 for a starting job; he starts only
in the weeks when fewer than two of the running backs ahead of him on *your*
roster are available, which the model knows from their expected games and their
byes. It is whether the lineup has room for him, not whether he plays: his own
bye and injury risk are already in `proj_points`. With a FLEX slot the
probability is a lower bound, since a player can also start through the flex,
which it does not count. `START_PROB_FLOOR` keeps it off exactly zero — a slot
also opens through a trade, a cut or a benching, none of which are modelled, and
the docstring already calls the result a floor rather than an estimate.
`who_should_i_pick` reports it as `starts_in_a_given_week` with `bench_value`
beside it whatever the weight is.

The term is applied to **`draft_score` itself**, before `recommend` computes the
fallback, the marginal value or either multiplier — not to `pick_value`
afterwards. A player in the lineup a fraction `m` of the time is worth `m` of his
projection, and `draft_score` is built from that projection, so that is the one
place the statement is true. Only value above replacement is scaled:
`draft_score` is value over replacement, not points, so a player already below it
is not made better by playing less.

Measured at pick 125 holding two backs who never miss a game, 560 of 575 rows
negative: a bench RB the term says can hardly ever start goes from `pick_value`
7.2 at **rank 3** to 0.5 at **rank 9**, while a receiver the term does not touch
stays at rank 1 with an unchanged 12.7.

**What this does not fix.** The value is cut by 93% and the rank moves six
places, because 0.5 is still ahead of the 560 negative rows.
`pick_value`'s zero means "exactly as good as waiting", not "worthless", so
anything positive outranks the whole negative field however small it is. Scaling
the position also scales what that position offers at your next pick — correctly,
since every candidate there is behind the same held players — so the marginal
value shrinks proportionally rather than going negative. Ranking a barely-starting
bench player against a starter is a property of comparing marginal-over-waiting
across positions, not of this term, and it is not solved here.

`handcuff` prices contingent upside. The direct backup at an NFL team and
position — depth rank 2 by the model's own projection — inherits the games the
starter is expected to miss at the per-game upgrade between them, and that many
points are *added* to his `pick_value`, doubled when you already hold the
starter and gated by the chance he would be in your lineup when the promotion
comes. Each of those is a correction the evidence forced. Added rather than
multiplied because a deep bench player's `pick_value` is negative, so scaling it
by his handcuff case pushes him further down; only depth rank 2, because giving
it to everyone behind the starter rewards whoever is worst (the gap to the
starter is widest for the man least likely to inherit anything); and gated,
because contingent points a roster can never start are not points — a QB2's
starter has the best per-game output on the board and the bonus lands after
`need_mult` has already discounted him.

The gate does **not** apply when the starter is yours. His absence is both what
pays the contingency and what opens the lineup slot — one event, not two — so
gating there squares a probability `contingent_points` has already applied.
Left in, it made holding the starter *lower* his handcuff's value than not
holding him, the exact reverse of the intent.

Neither roles term is a multiplier on `pick_value`: the start-probability term
scales `draft_score` before anything reads it, and the handcuff term is additive
in `pick_value`'s own units. The multipliers that remain (`need_mult`,
`role_mult`, `bye_mult`) all go through `model._discount`, which handles the
separate problem those have on the negative half of the board.

`just roles [what] [seasons] [trials] [seed]` is the evidence. `what`: `shares`
prints opportunity-share coverage on the live board and checks `pick_value` does
not move; `entropy` bins projection error by role entropy on a leak-free board
per season; `weights` runs the paired mock drafts behind both weights, and
`start_prob` or `handcuff` runs one of them. `seed` shifts the trial seeds, so a
second run extends a sample rather than repeating it. Numbers in
[CHANGELOG.md](../CHANGELOG.md).

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

Every row carries the four numbers the recommendation is made of, so the
comparison never has to be inferred from a rank: `value_now` (what taking him is
worth over replacement), `expected_best_at_next_pick` (what his position is
expected to still offer at your next turn), `marginal_now_vs_wait` (the
difference — negative whenever waiting is worth more), and `survival` (the
chance he is still there).

All three are rounded to one decimal **independently**, so subtracting the two
displayed components can disagree with the displayed difference by 0.1 on a row
or two. `marginal_now_vs_wait` is the faithful one: it is the unrounded quantity
the headline gate compares, rounded once for display. Deriving it from the
rounded components instead would make the reader's subtraction work and let the
printed number drift from the one that decided the headline, which is the
disagreement this whole section exists to prevent. `why_now` says all of it in words: *"55% likely still
there at 157; taking now is worth +3.9 over waiting"*, or *"only 22% likely
still there at 157; waiting is worth 1.0 more than taking now"*.

The `headline` is `"Take X"` only when there is a reason to hurry. A candidate
more likely than not to still be there, whose edge over waiting is under
`model.NO_URGENCY_MARGINAL` (5.0 points — under a third of a point a week, which
is inside the model's own noise; policy, not fitted), is reported as
`"No urgency; best available is X"`. This exists because a row reporting a 0.55
survival under a "Take" headline was read as *he does not come back*, which is
the reverse of what 0.55 means.

`roster_note`, and `roster_slot_note` on the rows it affects, appear when the two
halves of the model disagree about the same roster: the roster-need discount
counts your picks by position, the bench-value model reads the board rows behind
them, and one man is counted at a position and priced at none. Both notes name
the position and the counts, so a recommendation resting on that gap says so.

A pick the board cannot price at all — a kicker, a defense, a player with no
projection — no longer causes this; it is stood in at replacement level and the
two counts agree. What is left is narrower: the board carries a row for the
player and records no position on it. That is the board being wrong about
someone rather than silent about him, so the note reads as a defect report
against the board rather than a caveat about an unmodelled player.

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

### `stream_kdst`
Which kicker and defence to start or pick up **this week**, by that week's
matchup. Not by season projection, and not by the draft's counting model — that
one answers whether a required position can still be filled at all (`#26`/`#32`)
and is not consulted here. Ranked on the implied points the book has posted: a
defence wants an opponent expected to score little, a kicker wants his own
offence expected to score a lot. `look_ahead` weeks come back beside this one so
a waiver claim can be judged against the bye it has to cover.

**The asked week returns the top `top` rows per position, 8 by default**, with
`ranked_of` giving the size of the field behind them. The look-ahead weeks carry
only `name`, `opponent`, `score` and `line_basis`, because the only question they
are there to answer is whether a bye is covered and by whom. Calibration collapses
to its verdict. `detail=true` restores the whole field, the full look-ahead rows
and the evidence behind each verdict — at `look_ahead=2` that exceeds the client's
limit and comes back trimmed with a `truncated` field naming what was cut, so pair
it with `look_ahead=1`.

**Read `margin_units` per position before reading any margin.** The score is
calibrated against real results under this league's own K and D/ST bands, in two
disjoint blocks of weeks, and a margin ships in points only when *both* hold:
every coefficient keeps its sign across the blocks, **and** each block predicts
the other better than that block's own average does. Sign agreement alone is not
enough — a fit can agree with itself and still be worse than guessing the mean,
which is exactly what kickers do. Where either test fails the ranking still
stands and the margin is withheld rather than dressed up as points.

On the current data: **defences calibrate** (signs agree, variance explained
0.186 and 0.125 across blocks, coefficient spread 0.13) and ship points.
**Kickers do not** (signs agree but variance explained 0.02 and −0.014, so one
block is worse than its own mean) and ship ordinal.

The rule is not restated here and enforced somewhere else: `adp.margin_unit`
decides it and is the only place the word `points` comes from, so a tool that
cannot show its evidence cannot label a number with a unit. `margin_units_reason`
beside every verdict names which clause failed, because "the evidence disagreed"
and "the evidence was never gathered" read the same in an answer and are not the
same thing.

`line_basis` on every row says whether the book had posted a line for that game.
Lines cover the whole board about six weeks out and thin to nothing after week
seven, filling in as each week approaches; a row without one is ranked on what
remains and **never** given a season number in its place. Weather is not
available at all — `temp` and `wind` are recorded after kickoff and only
outdoors, so no future game has them from this source and only the stadium roof
is known in advance. `just stream <week> [league_id] [look_ahead]` prints it.

### `draft_retrospective`
Your draft, pick by pick, against what the model would have taken. Each of your
picks is replayed twice through the same walk `draft_replay` uses: once priced
from the market snapshot the watch recorded at that pick, once from today's
board. Per row: what you took and its projection, what the model would have
taken and its projection, the model's rank of your pick, the delta, and
`room_around` — the picks either side of yours, so a run or a reach is visible.

**Read `as_of_coverage` first.** Snapshots exist only from the pick at which a
watch first connected, so earlier picks cannot be priced as of the time and fall
back to today's board; `basis` says which, on every row. On the live draft that
is 2 rows of 9. A retrospective that mixed the two silently would be telling you
what today's market thinks of a decision you made against a different one.

`as_of_agreement` guards the opposite misreading: while the board has not been
repriced under the stored snapshots, the as-of and today columns are identical,
and that means the market has not moved — not that the as-of path did nothing.

`your_pick_edge` is your pick's projection minus the model's, so a positive
number means **your** pick projects more — signed so that up is good, and named
for whose edge it is rather than "delta", whose direction a reader has to look
up. It is a **projection, not a result**.
`your_pick_edge_actual` is the same comparison on real box scores and is null on every row
until the season has played a week; `delta_basis` names which of the two the
table is standing on and switches by itself once `weekly_stats` carries the
current season. `just retrospective [league_id] [slot]` prints the table.

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

The position intercepts on their own were evaluated separately (`just blendpos`)
and also not adopted. They are worth about 0.06 of log loss in the right
direction, consistently across 7 of 8 round blocks and both halves of the draft
— but the spread between round blocks is 0.30, five times the effect, the blocks
do not agree in sign, and a round-level bootstrap run as two disjoint seed blocks
puts the 95% interval on either side of zero depending on the seed. The rank
metrics move by 0.025 or less against round spreads of 0.07 to 0.29. One
recorded draft cannot resolve a difference this size; `just blendpos` is the
reproducer if a second draft ever makes it resolvable.

### `draft_strength`
Every team's draft so far ranked by projected starter points: the best lineup
its picks fill under the league's starting slots, bench projection, starting
slots still open, and pick count. Names come from the running watch when
`league_id` is given; otherwise teams are labelled by slot. Picks the board
cannot model fill their slot at 0 points.

### `evaluate_trade`
Score a proposed trade for both sides over the rest of the season. `give` and
`get` are comma-separated names: `give` leaves your roster, `get` arrives on it.
`counterparty_slot` is their draft slot; with a running watch for `league_id`
both teams are named.

**Known blocker on the live league.** A roster player the board cannot price
stops the whole evaluation, whether or not he is part of the trade: on the
current record MarShawn Lloyd has no board row, so every call returns
`no board row for: MarShawn Lloyd` and no trade can be scored at all. Refusing
when a *traded* piece cannot be priced is deliberate — a trade scored without one
of its own pieces is a different trade — but a bystander on the roster is a
different case, and `board.my_rows` already answers it by standing such a player
in at replacement level. Measured, not inferred: `just payloads` reports the
refusal and re-prices the same trade against a board carrying stand-ins.

Each side's roster is simulated week by week on its own starting lineup, and
each side is reported as points before and after, per-position depth before and
after, and a verdict. The counterparty also gets their draft record's
tendencies: picks by position and `mean_adp_delta`, which is positive when they
let players fall to them and negative when they reached.

Both sides can gain. The same player is worth different points to two different
lineups, so a swap of two teams' surpluses improves both; two sides both losing
is the tell that the trade empties a starting slot somebody was filling.

Every estimate arrives beside `block_spread`, the distance between disjoint seed
blocks of the same configuration, which is this harness's own noise. **A side
whose blocks disagree in sign is reported as no call, not as even**: that
difference has not been measured. Agreement is not a pass either, and
`blocks_agree_p_null` says what it is worth — at the default of two blocks, one
coin flip.

`unit` says whether the gain may be read as a quantity of points, and it comes
from `adp.margin_unit` rather than from this tool. The calibration rule's second
clause — each block beats its own mean out of sample — asks whether a *fit*
generalises, and this harness fits nothing: its blocks re-run one simulation on
disjoint seeds and its inputs are already in points, so there is no held-out set
for the clause to be tested on. It therefore goes through the rule's declared
`replication` path, which grants points on sign agreement **and** on the caller
naming the unit its inputs carry. Undeclared is ordinal, like anything else, and
`unit_reason` says which door the answer came through.

What is simulated: a week is the player's per-game rate if he is available, and
0 on his bye or when his availability draw fails. Availability comes from
`roles.weekly_availability`, the same mapping the board's own `exp_games` feeds.
The board's `proj_points` is `adj_ppg * exp_games`, so paying `proj_points` per
week would charge the injury risk twice; it does not. Week-to-week scoring
variance is deliberately **not** modelled: the board carries no distribution for
it, and inventing one would move the very spread the reader is meant to judge
the estimate against. Kicker and defense slots are not scored, which is
`best_weekly_lineup`'s existing behaviour.

`priced_by` says how each side's rows were priced. A player the board models is
priced from `adj_ppg`; one it does not, a kicker or a defense, has his per-game
rate derived from `proj_points / exp_games`, and one with no projection at all is
worth 0. Every row that is not straight off the board is **named**, not merely
counted, because a delta built from derived rows deserves less weight than one
built from modelled ones.

`weeks` reports the window scored, `from` 1 `to` 14. Rosters come from the draft
record, so this is a season-long answer; a trade weighed in week 9 is really
asking about weeks 9 to 14, and the tool does not yet know the difference. That
arrives with the in-season roster reader.

A player added after the draft is not on the record yet. Naming a player on the
wrong roster, or one with no board row, stops the evaluation and says which: a
trade scored without one of its own pieces is a different trade.

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
per block, two blocks per season, weight 0.08, measured at 854b5da) found why.
Per season, the two blocks: 2022 **-3.3 / +4.7**, 2023 **+5.9 / -4.6**, 2024
**-27.7 / +4.1**, 2025 -19.3 / -35.1; overall -9.4, `blocks_agree` false. Three
of the four seasons have two blocks of the same configuration pointing opposite
ways, by 8.0, 10.5 and 31.8 weekly points, so no magnitude here is supported by
anything. Only 2025 agrees, and one season agreeing is one coin flip
(`blocks_agree_p_null` 0.5). The penalty is close to inert in any case, changing
8, 7, 11 and 11 rosters out of 24 per season, so it stays informational like
`matchup_z`. Kickers and defenses are not modelled, so their byes are yours to
check.

### `bye_backtest`
Does the bye-week stacking penalty win more weekly lineup points? For each season
and seed, one mock draft with `bye_weight` 0 and one with the given weight, same
bots and noise, scored as the best legal lineup each regular-season week on real
box scores. Season totals cannot see a bye; weekly lineups can. `improvement` is
weekly points gained per season; `empty_slots` counts starter slots nothing could
fill. `just bye [seasons] [trials] [weight] [seed]` prints the same without a
server.

**Read `improvement` against `block_spread`, never on its own.** Every paired
backtest here runs `blocks` disjoint blocks of `n_trials` (default
`adp.DEFAULT_BLOCKS`, seeds `seed + block * n_trials + trial`) and reports each
block's own improvement in `blocks`, their range in `block_spread`, and whether
they point the same way in `blocks_agree`. The spread between two blocks of the
*same* configuration is the harness's own noise for that term: running both
`roles.py` weights together over 2024 gave +18.4 weekly points on seeds 0-11 and
-21.3 on seeds 8-19 while changing 40 rosters, where the bye penalty over 2022
gave +8.1 and +6.3 while changing 5. A term that rarely fires has little noise to
make, so read `block_spread` beside `trials_changed` rather than treating any one
spread as a universal floor. When `blocks_agree` is false the improvement is
inside that noise and supports nothing, whatever its sign.

**A true `blocks_agree` is not a pass.** Two blocks of a term that does nothing
agree in sign half the time, so agreement at the default is one coin flip.
`blocks_agree_p_null` carries what it is worth — 2^-(k-1) for k blocks, so 0.5 at
two and 0.125 at four — and sits beside the flag so it cannot be read as
confirmation. Raise `blocks` when the answer has to carry weight. A run of this
length can reject a term that is badly wrong (the ungated handcuff term stayed at
-103.5 in every block); it cannot confirm one that is mildly right.

`trials_improved_of_changed` is the win count over the trials the weight
actually changed, beside `trials_changed`. About half the paired trials draft
the identical roster, and counting an abstention as a loss drives any
conservative term toward a 50% win rate — the difference between "4 of 12
trials improved" and "4 of the 6 it changed". Its denominator is the trials the
change fired on, which is a post-treatment variable, so it is not another view of
`improvement` and the two cannot be reasoned about together.

### `draft_queue` / `set_draft_queue`
Your ESPN pick queue, the list autopick draws from if you miss the clock.

**The queue has two authors and ESPN's protocol has no add or remove.**
`DRAFT_LIST id id ...` carries the whole list, so anything a call does not send
is deleted. The user edits this queue in the ESPN app; the server edits it here;
neither sees the other's change except as a new echo of the entire list.

`set_draft_queue(league_id, "Name, Name, ...")` therefore **merges** by default:
it reads the queue ESPN last echoed, puts the named players at the front in the
order given, and keeps everything else the user had behind them. The result
reports `added`, `kept_from_the_users_queue`, `removed` and `queue_before`, so
what happened to the user's own list is on the face of it.

`replace=True` sends only the named players and drops the rest. It is the old
behaviour and now has to be asked for; its result names every player removed.

With no echo yet on this connection the queue ESPN holds is **unknown**, and a
merge is refused rather than guessed — sending anyway is how a queue the user
built gets overwritten with nobody able to say what was in it. The error carries
the way out and its cost in one sentence, and `replace=True` still works because
it was asked for.

**The INIT snapshot's `draft_list` is empty**, so the queue does not arrive with
the join frame. It arrives as a `DRAFT_LIST` echo shortly afterwards, unprompted:
3.7 seconds after INIT on the 2026-09-05 join. So `set_draft_queue` **waits** for
that echo, up to ten seconds, before refusing anything — the refusal is what is
left when the echo genuinely never comes, not the normal outcome of calling early
in a connection.

The queue belongs to **one connection**. `run()` reconnects, and ESPN drops the
queue when a client session ends, so the watch clears it on every connect and
`draft_queue` reports which connection each echo came from. A list that shrank
across a connection boundary was not necessarily edited by anyone.

`draft_queue` returns two lists. `as_echoed` is what ESPN last sent, verbatim,
with `drafted_at` on every row naming the pick that took that player or `null`
if he is still available. `effective` is the same queue minus the drafted, which
is what autopick would actually draw from, renumbered.

Both are needed because **ESPN sends no `DRAFT_LIST` when a pick empties a slot
in your queue**. At pick 135 the echo still listed a player taken thirteen picks
earlier. Autopick skips him, so nothing breaks, but the echo alone states a queue
ESPN will not use. `drafted_since_the_echo` counts the difference.

It also returns `echoes`, every echo seen with a timestamp and a connection
number. Since ESPN sends the whole list rather than a change, comparing
consecutive echoes is the only way to answer "when did this player leave my
queue". Those rows are **not** annotated: they record what ESPN said at the time,
and marking them with what has happened since would make a log of the past
disagree with itself. The key is **absent** on them, not `null` — a null on a row
nobody checked is a claim that the player is available.

`pick_log` says whether the log behind the annotation could be read, **on success
as well as failure**. A field that appears only when something went wrong makes
its own absence the signal, which is the thing stating `drafted_at` on every row
exists to avoid. When the log cannot be read — no INIT on this connection, or a
decode that failed — `drafted_at` is absent, and `effective` and
`drafted_since_the_echo` are `null` rather than a guess. Repeating the echo there
would restate the claim that every queued player is available, and `[]` would say
the queue is empty.

`set_draft_queue` annotates its lists the same way, `removed` above all. `removed`
and `kept_from_the_users_queue` are computed from what ESPN echoed back, never
from what was sent: ESPN drops ids it rejects, and an already-drafted player is
the ordinary reason, so a merge that intended to remove nothing can still lose one
of the user's players. `drafted_at` on those rows is the reason the list needs.
`would_send`, on the refusal path, likewise says which of the queue the caller
meant to send is already gone.

**An observation, deliberately unconsumed.** On the 2026-09-05 join, in a snake
draft, `INIT.draft_list` was empty but `INIT.nomination_list` held this team's
ten players in an order matching the first echo exactly. **n = 1**, and
`nomination_list` is named for auction nominations, so that is evidence it is
some ordered list belonging to this team, not evidence of what ESPN writes there
in a draft where the two could differ. Seeding the queue from it would remove the
wait entirely, and it is not done: if the reading is wrong, the tool would merge
against a list that is not the user's queue, silently, which is the exact harm
this design prevents.

Instead the watch reads it, uses it for nothing, and records whether it matched
that connection's first real echo. `draft_queue` returns those checks as
`init_queue_checks`, so the evidence accumulates across every connection anyone
runs and the question can be settled on data rather than on one decode. A
disagreement would surface before anything depended on it.

### Watch resume across a server restart
Every `/mcp` reconnect starts a new server process, and the old one's socket,
watch and merged queue die with it. `watch_draft` now records its intent under
`~/.ffdraft/watch/<league_id>.json` (or `$FFDRAFT_WATCH`): league, team, season,
the queue as ESPN last accepted it, and a resume flag. The next process rejoins
every recorded room before anyone asks.

The queue is re-sent through the **merge** path, so anything the user has queued
in the ESPN app since the old process died is kept. That path waits for ESPN's
own echo first, which is what makes re-sending a minutes-old queue safe.

**With no echo it re-sends the record as a replace instead of sending nothing.**
ESPN drops the pick queue when the client session ends and sends no `DRAFT_LIST`
while it holds none, so the echo the merge waits for never comes on the one path
resume exists for. Measured on the first live use: the watch came back, the
message said the queue was NOT re-sent, and the user's queue stayed empty until it
was sent by hand. There is nothing of the user's to overwrite when ESPN is holding
nothing, and what goes out is the queue they had before the restart, not one this
server chose. The message says so, and names the case where it is wrong: a queue
edited in the app during the downtime that ESPN then failed to echo. Once an echo
has arrived the merge stands and a replace is never used.

It re-sends **ids**, not names. `set_draft_queue` is a name-resolving front over
`merge_queue_ids`, and only the front resolves names, because `resolve_espn_id`
gates on the crosswalk: a player on the board but missing from it — a kicker, a
rookie, a mid-draft callup — comes back unresolved and refuses the entire send.
Those are exactly the players a merge preserves, since they never went through
the crosswalk to begin with.

It does **not** resume when `stop_watch` cleared the flag, when `mDraftDetail`
says the draft is complete, when the record is more than 24 hours old, when a
watch for that league is already running, when the watch stops before ESPN sends
the draft state, or when the record cannot be read. **Every one of those is
announced on the channel** except the user's own `stop_watch`, which they already
know about and which would otherwise be noise on every start forever. A watch
that silently does not come back is the same problem as one that silently dies.
A refusal also leaves nothing in place: `draft_room` and `draft_status` answer
from the same registry, so a watch the user was told does not exist must not
still be answering questions.

A slow join is **not** a refusal. If ESPN has not sent the draft state within 30
seconds the socket is still up and picks are still being recorded, so the message
says so — missed picks are the one loss a draft cannot recover from, and killing
a live watch to make a failure message true would trade it away. The queue is
re-sent, and the usual resume line follows, whenever the state does arrive.

One channel event reports success: `watch resumed after restart: N picks made,
your next pick is P; queue re-sent, K entries, M of them yours`. **It arrives
with the first tool call, not at start.** There is no session at server start —
sessions are built per request, and what outlives them is the connection's
standalone channel — so the message is held until one exists. The socket is live
from the moment it resumes either way; only the telling waits. A message held
more than a minute is stamped with its age, because a held message is not a
delayed message: its subject has moved.

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
payload raw (`init.b64`) and decoded (`init.json`), the picks it carried with
their draft slots (`picks.json`), and `lines.jsonl`: every socket line with a
receive timestamp in ms. From a running watch that is every line since it
joined, the only timestamped record of picks that exists; `just dump` opens the
room once instead and captures the join burst only, bumping a browser room or
watch.

`INIT` is initial state: the socket sends it on join and never resends it, so
those three files describe the draft at the moment the watch joined, however
long ago that was. `live/state.json` is the draft **now** -- the snapshot
replayed through the `SELECTED` and `UNDONE` lines since (`espn_live.replay_picks`,
the arithmetic the watch runs on live state). Each row carries `source`: `init`
or `selected`. Every `live` entry in the manifest states the pick count it is
as-of, so the join number and the current number are never read as the same
figure; `state.json` also reports `events_applied` and `events_unparsed`.

`live/queue.json` is your pick queue as ESPN last echoed it (`DRAFT_LIST`, the
whole queue after any add, remove or reorder). It is the other piece of live
state that exists nowhere else: `INIT` does not carry it and the read API never
sees it. A `queue` of `null` means ESPN echoed none on this connection, which is
not the same as a cleared queue (`[]`). A `DRAFT_LIST` changes nobody's picks,
so it is its own file rather than an input to the pick replay.

`live/reconcile.json` compares the current state against `read_api/mDraftDetail.json`
and its `status` is one of:

| status | meaning |
| --- | --- |
| `clean` | every pick agrees on player and team |
| `mismatch` | listed under `missing_from_read_api`, `missing_from_live`, `disagreements`; also raised in `errors` |
| `blind` | the read API returned every slot at `playerId` -1, which is what it does until the draft completes. Normal mid-draft, not a disagreement |
| `unreadable` | `mDraftDetail.json` is missing, unparseable, or has no `draftDetail` |
| `no live state` | no watch and no `team_id`, so there was nothing to reconcile |

### `record_pick` / `undo_pick` / `reset_draft` / `draft_status`
Manual board management. `record_pick` accepts shorthand.

### `plan_my_draft`
Simulate every remaining pick from your slot. `strategy`: `balanced`, `zero_rb`,
`hero_rb`, `robust_rb`. ADP-driven, so treat it as preparation rather than a script.

Availability at each simulated pick comes from the same survival model
`who_should_i_pick` uses, conditional on the player being on the board now, not
from a separate ADP rule. That is necessary and not sufficient: the survival
model is per player, and a kicker or defense carries an ADP in the middle of the
draft that no per-player rule keeps alive to the last rounds, so both positions
used to vanish from the pool entirely and a 14-round plan for a league starting
both finished with neither. A position cannot be emptied for you while more of
its players remain than the rest of the league can absorb, so a required position
is answered by counting at every turn rather than by the filter: the league needs
`starters * teams`, some are gone already, and the remainder is absorbed evenly
over the picks that are left, so the plan is offered the best one after those.
That count is anchored on what has actually happened, so it is exact at the
current pick and rises monotonically — the offered player can never improve as
the draft goes on, which it used to do, jumping from the best defense to the
sixteenth the turn the filter emptied the position. And with no more picks
remaining than empty required slots, the pool is restricted to the positions that
fill them:
`draft_score` is value over replacement, so the last startable kicker scores
about zero by construction, and only the comparison against an empty slot is the
one available at the final pick.

## Research

### `player_report`
Every modelled factor for one player: production, role, environment multipliers, injury
components, separation, draft capital for rookies. Includes red zone role
(`rz_touches`, `rz_td`, `rz_td_rate`) against the position's baseline conversion rate
(`rz_touches`, `rz_td`, `rz_td_rate`) against the position's baseline conversion rate
(`rz_baseline_rate`) and the resulting `m_td_luck` multiplier — surfaced in the plain-
language `summary` as "touchdown regression" whenever it moves the projection.

**Opportunity, named** (`roles.py`): `target_share`, `carry_share`, `redzone_share`
and `snap_share`, each of the player's own team's total that season and
recency-weighted the same way production is, so "800 yards on 105 targets" and
"800 yards on 60 targets" stop reading alike. Red zone share is his share of his
team's plays inside the 20, taken from the play rows, so a player who changed
teams is measured against whoever he was playing for at the time. The `summary`
prints them as "share of team: targets 21%, carries 0%, red zone 11%, snaps 85%".

**Role entropy** (`roles.py`): `role_entropy` in [0, 1], with the two parts it is
made of. `proj_disagreement` is |ln(ESPN projection / model projection)| — zero
when they agree, symmetric, full at a factor of two. `role_churn` is the
week-to-week coefficient of variation of the player's share of his team's
offensive snaps in his most recent season, full when its standard deviation
equals its own mean; under six appearances it is left blank rather than guessed
from three games. The score is their mean. `entropy_kind` names the direction,
because uncertainty is not one thing: ESPN projecting *above* a model built from
past production is `unresolved upside`, ESPN projecting *below* it is
`role in doubt`.

`entropy_basis` names which halves a row's score rests on — `disagreement+churn`,
`disagreement only` or `churn only` — because the two do not have the same
evidential standing. The churn half is monotonic against real projection error in
two seasons across 700 players; the disagreement half has no test of its own
here. Both components are reported beside the blend in `player_report` and
`who_should_i_pick` so a consumer can use the evidenced half alone, and
`explain()` names a one-sided basis. Two projections that are bit-identical are
one number rather than two that agree, so a kicker or defense priced from ESPN's
own projection scores no disagreement at all rather than reading as the most
certain role on the board.

Nothing in `pick_value` depends on any of it — they are read-only columns; see
**Role weights** under `model_settings`.

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

## In season

### `waiver_targets` / `just waivers <week> [league_id] [limit]`
Who to claim off waivers this week, at what priority, dropping whom. Weeks 1-15.
The pool is ESPN's `kona_player_info` with ownership; the settings are
`mSettings`.

A player is on the list for one of two named reasons and `reason` says which:
his **role moved** — snap share and target share over the last
`waivers.RECENT_WEEKS` (2) against the `waivers.PRIOR_WEEKS` (3) before them —
or a **starter ahead of him is out**. The two are listed rather than traded off,
because trading them off needs a rate nobody has measured. A player with neither
reason is not a claim and is not listed.

`census` is what makes an empty list readable: `considered`, `with_weekly_usage`,
`role_moved`, `starter_out`, `claimed`, and a named `status`. A quiet week and a
broken free-agent pull both produce `claims: []`, and those are the two most
different answers this tool has; `status` tells them apart rather than leaving
it to be inferred from a zero.

**Three of the four scores are `unmeasured` and every row says so.** Role
change, projection lag and contingent value have no backtest, so the ranking is
by a quantity whose predictive value is unknown; `evidence.role_entropy` alone
carries a real result. Ordering is role-movers by `role_change` first, then live
contingencies by `contingent_value` — weight 1 on one term and 0 on the rest,
which is a stated policy about which question to read first, not a measurement.
The four numbers stay in the row where a human can override them rather than
being blended into one score nobody can decompose. What licenses changing that
is the role-change backtest, not a preference.

`shape` marks `free_agent_pool` and `ownership_move` `unverified-shape`: the
capture these were written against was taken mid-draft, when ESPN reports every
player as a free agent, so the split this selects on has not been exercised
against a real in-season pull.

**Claim priority is a waiver order, not a bid**, when `isUsingAcquisitionBudget`
is false — which it is in this league. `acquisitionBudget` (100) and
`minimumBid` are populated and inert beside it, and reading those first is how a
tool recommends FAAB to a league that does not use it; `faab_bid` is null unless
FAAB is actually on. **Every claim names a drop**, because `isBenchUnlimited` is
true while `lineupSlotCounts["20"]` is 6 — the slot count is the fact.

The drop is the lowest `bench_value` among the players `lineup.droppable` says
the league's slots are filled without — **by position against `league.starters`,
never by board rank**. The two are not the same list. `DraftState.my_rows`
returns board order, which is rank order, so taking the bench as "everything
after the first N rows" means "outside my top N by rank"; that diverges from
"not a starter" the moment a roster is unbalanced across positions, and rosters
are always unbalanced because people draft best available. On an ordinary
receiver-heavy roster it called TE1, K1 and DST1 the bench, and since a kicker
and a defense carry the lowest projections on any roster by construction, the
tool would offer the user's only defense as the drop — in a row that
simultaneously reported `starts_in_a_given_week` 1.0. Found by marge on review;
`lineup.starting_lineup` is the shared answer and #44's `set_lineup` asks the
same question of the same function.

`bench_value` prices the weeks he would actually start rather than his
projection, so a fourth running back who projects well is still a fourth running
back. The drop is checked against ESPN's undroppable list (`player.droppable`);
a player the pull did not carry is offered with `undroppable_checked` false
rather than assumed droppable, and a bench row the board cannot price says so in
`projection_basis`. A roster row carrying no usable position is named in
`unplaceable_on_my_roster` rather than treated as spare: it matches no slot, so
it can never be a starter, so a bench taken as the complement of the starters
would swallow it — and it may be the only kicker on the roster with a broken
board row.

`starters_out` reads "is out now", not "changed this week" — detecting a change
needs last week's statuses and nothing stores them yet, so the claim is the
weaker one and is labelled as such. `QUESTIONABLE` is deliberately not an out
status: by Friday it describes half the league and would make every backup a
handcuff.

## Environment variables

| Variable | Purpose |
|---|---|
| `FFDRAFT_SEASON` | Season being drafted (default 2026) |
| `FFDRAFT_SEASONS` | Override lookback, e.g. `2021,2022,2023,2024,2025` |
| `FFDRAFT_CACHE` / `FFDRAFT_DATA` / `FFDRAFT_STATE` | Storage paths |
| `ESPN_SWID` / `ESPN_S2` | Private ESPN leagues — see [SECURITY.md](../SECURITY.md) |
