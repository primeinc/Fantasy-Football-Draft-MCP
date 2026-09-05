# Methodology

How the numbers are produced, and why several defaults are what they are. Most of these
were calibrated against data after a first attempt produced something visibly wrong;
where that happened it's noted, because the failure explains the choice.

## Projection pipeline

### 1. Baseline

Recency-weighted fantasy points per game across five seasons (40% last year, decaying to
5%), computed under your exact league scoring rather than converted from a published
number.

Small samples regress toward the mean of **starter-caliber** players at the position —
the top 36 backs, top 48 receivers and so on — not toward the mean of everyone who
logged a snap.

> This one was wrong first. Regressing toward the all-player mean pulled the target down
> so far that a genuine RB1 lost about a third of his projection: Bijan Robinson came out
> at 189 points, when the real figure is around 250.

Shrinkage is on games played, not seasons. Seventeen healthy games is a real sample;
three cameos across two years is not.

### Team assignment

Before any environment multiplier is applied, each player's team is reconciled
against the current official depth charts, not just his last known team from box
scores. Box-score history only updates once a player has played a game for a team,
so a trade, cut or free-agent signing is invisible there until Week 1 — which is
after most drafts happen. Depth charts are filed by the teams themselves and update
through the offseason, catching a move as soon as it's reported. This matters beyond
the label: O-line, pace and schedule strength below are all looked up by team, so a
stale team would grade a traded player on his old offense. A player missing from the
depth chart (a rookie before camp, or before this season's chart is published) just
keeps his last known team.

### 2. Environment multipliers

Each is bounded by a configurable weight, so no single factor can dominate.

**Offensive line.** Run block from Adjusted Line Yards — rushing yards credited to the
line on a sliding scale, because the line owns the first four yards and the back owns
the breakaway. Pass block from sacks and QB hits allowed per dropback. Backs get run
block; passing-game players get pass block.

**Pace and run/pass split.** Plays per game plus *neutral-script* pass rate, measured
only when win probability is between 20% and 80%. Raw pass rate mostly tells you whether
a team was losing.

**Schedule.** Recency-weighted fantasy points allowed by every opponent on next season's
slate, computed per position defended. Divisional games are counted separately — you
face those six defenses twice and can't escape them.

**Injury.** Availability history, injury-report frequency even in games played, and
workload burden. Heavy touch volume past the positional age cliff compounds, which is
where running back seasons tend to go wrong.

**Age.** Positional aging curves — backs decline from 27, receivers from 30.

**Separation.** For pass catchers only, and only those clearing the route threshold.

**Touchdown luck.** Red zone touches and touchdowns, from raw plays rather than the
season touchdown total box scores publish. A player's own red zone conversion rate
is regressed toward what his position converts on average, computed from the same
starter-caliber cohort `pos_target` above uses, not the whole league. Touchdowns are
the noisiest thing a player does — worth more to the score than anything else, and
far more volatile year to year than yardage — so this is deliberately conservative:
below 8 red zone touches a rate is a coin flip and sits neutral, and even above that
threshold the correction is bounded like every other multiplier here, not a full
reset to the baseline.

> For example: Justin Jefferson's five-year red zone role converts at 16.9%, against
> a receiver baseline of 25.3% computed from the same starter cohort — real target
> volume (145 touches/season recency-weighted) that hasn't translated into
> touchdowns at the rate his peers' does. `m_td_luck` lifts his projection 3.6% for
> it, a small correction bounded by `td_luck_weight` like every other factor here,
> not a claim that he's about to lead the league in scores.

### 3. Consistency

The floor score: how often a player delivers a usable week. Startable rate (45%), floor
as a share of average (25%), inverted variance (15%), availability (15%).

Regressed for small samples using the same shrinkage as the projection.

> Also wrong first. Without the regression, a fringe receiver with three good games
> showed near-perfect reliability and ranked 16th overall on "consistency" he had never
> demonstrated across a real workload.

### 4. Value over replacement

Replacement level is the last startable player at each position given your league size,
plus realistic bench depth. It scales with team count, which is the point: the last
usable back in a 10-team league is a far better player than in a 13-team league, so the
same player is worth less over replacement in the smaller one.

Superflex adds roughly `0.9 × teams` to the quarterback baseline, since nearly every team
starts two.

VOR is then blended with consistency at your chosen weight.

## Draft logic

### Positional opportunity cost

The step that actually decides picks. Raw value says take the best player. That's wrong
in a snake draft: passing on an elite quarterback costs almost nothing, because the one
you get two rounds later is nearly as good, while passing on an elite back costs a great
deal.

For each position the model walks the board top-down, accumulating the probability that
every better player is already gone. That product is the chance a given player is the
best one left, and the sum over players is the expected value of waiting. A pick is worth
its **marginal gain over that expectation**.

> Tested at pick 6 with the top five off the board, this recommends Amon-Ra St. Brown
> over Josh Allen despite Allen grading as the highest-value player on the board — Allen
> has a 73% chance of lasting to pick 19, and St. Brown does not.

### Survival probability

ADP is treated as the centre of a distribution whose spread widens later in the draft,
matching how real variance behaves: pick 3 goes where pick 3 goes, pick 90 is a coin flip
across twenty names. Every number reported is conditional — the chance he lasts to your
next pick *given* that he is on the board now.

The shape of that distribution is logistic, not normal (`model.SURVIVAL_TAIL`), with the
same spread. Only the tail differs, and the tail is what the model is asked about most
often. A Gaussian right tail says a player three and a half standard deviations past his
ADP is certainly gone; real boards are full of players who are not. An exponential tail
instead makes the conditional survival of a player well past his ADP tend to a constant
hazard per pick: "he has slid this far already, so the chance he goes in the next seven
picks is about what it was for the last seven", which is the right statement about a
player the market has stopped pricing at his ADP.

Evidence, the recorded 2026 draft replayed at 122 picks, same board and picks with only
the shape varying. Overall survival Brier 0.129 -> 0.127 and log loss 0.416 -> 0.401
against a base rate of 0.250, with log loss improving in five of seven rounds and for QB
(0.836 -> 0.760), WR (0.289 -> 0.275) and K (0.187 -> 0.170), unchanged for RB and TE,
and worse for DST (0.568 -> 0.618) on 17 forecasts.

Those aggregates are direction, not proof, and the change does not rest on them. This is
one draft. Treating the seven rounds as blocks, the per-round log-loss deltas are -0.080,
-0.014, -0.001, +0.014, -0.019, +0.007, -0.014 — mean -0.015, t = -1.3 on 6 degrees of
freedom; Brier gives t = -1.6; dropping round 1, which contributes more than half the
total, leaves t = -0.8. Nothing there is distinguishable from zero.

What the change rests on instead is that the old answers were wrong independently of any
score. A defense demonstrably on the board at pick 123 was assigned a survival of 0.00,
and past about eight standard deviations the arithmetic returned 1.0 for a player who was
certainly gone. Supporting that: the mechanism, and the lowest-probability bucket, where
the normal predicted 0.040 against 0.080 observed and the logistic predicts 0.050 against
0.070 — half the calibration error, on 329 and 334 forecasts, in the exact region the
change targets.

The per-position counts differ between the two runs because the replay re-derives its
recommendations from the survival numbers themselves, so the position breakdowns are not
paired samples and the small ones should not be read as if they were.

### Roster need

A bench player is worth the odds he ever starts for you, decaying fast at one-slot
positions. Hard caps shut a position off entirely past the point it can help.

> Before the caps, the simulator drafted eight quarterbacks. The old floor of 0.62 was
> nowhere near enough to stop a position whose marginal value kept recovering as its
> better players were taken.

In superflex the second quarterback is a starter rather than a bench body, so those
rules invert.

### Start probability and contingent upside (both off by default)

`roles.py` prices the same idea properly rather than by decay, for a league with no
FLEX slot. An RB3 does not compete with a WR3 for a starting job: he is in the lineup
in a week only when fewer than two of the running backs ahead of him *on your roster*
are available, which follows from their expected games and their byes. The count is an
exact Poisson-binomial over the men ahead, averaged over the fourteen fantasy weeks;
per-week availability is `exp_games / 17`, the same injury mapping `project` already
uses, so the two cannot drift apart. Exact with no FLEX slot, a lower bound with one.

A backup's contingent value is `P(role change) x delta value`: the direct backup at an
NFL team and position inherits the games the starter is expected to miss at the
per-game upgrade between them, doubled when you hold that starter, because the
contingency then covers a slot the roster already depends on.

Both are informational. `roles.weight_backtest` ran the paired mock drafts (the same
machinery as `bye_backtest`: same seed, same bots, same noise, scored on real weekly box
scores) and neither weight earned a place in `pick_value`. Start probability came out
-10.6 weekly points in 2024 and +20.6 in 2025 across 12 paired drafts each; opposite
signs by season is not evidence. The handcuff term came out +17.5 and +20.2 across 20
paired drafts each, positive in all four block-seasons, which is the only consistent
sign either weight produced. Those numbers and the two redesigns the handcuff term
needed before it was worth measuring at all are in [CHANGELOG.md](../CHANGELOG.md).
`who_should_i_pick` reports `starts_in_a_given_week`, `bench_value`, `handcuff_for` and
`contingent_points` per candidate without pricing any of them.

**What this backtest can resolve.** Running both weights together gave +18.4 on seeds
0-11 and -21.3 on seeds 8-19 — same configuration, same season, opposite signs, a
40-point spread. A single run of that length can reject a term that is badly wrong (the
ungated handcuff term at -103.5) but cannot confirm one that is mildly right, so every
paired backtest here now runs two disjoint blocks and reports both.

Two things that follow, and neither is optional when quoting a number from it. The
spread belongs to the term as much as to the harness — the bye penalty over 2022 gave
+8.1 and +6.3 while changing five rosters, against forty for the roles weights — so read
`block_spread` beside `trials_changed` rather than treating one measured spread as a
universal floor. And `blocks_agree` being true is not a pass: two blocks of a term that
does nothing agree in sign half the time, which is what `blocks_agree_p_null` reports
beside it.

One reporting detail that changes the reading: about half the paired trials draft the
identical roster, because the weight changes nothing at that seed. `weight_backtest`
counts those as ties rather than losses (`trials_improved_of_changed`), which is the
difference between "4 of 12 trials improved" and "4 of the 6 it changed".

### Role entropy (informational)

An uncertainty score in [0, 1]: the mean of |ln(ESPN projection / model projection)|,
full at a factor of two, and the week-to-week coefficient of variation of the player's
share of his team's offensive snaps, full when its standard deviation equals its own
mean. Both scales are policy, chosen to be quotable as a statement about the world
rather than fitted to a distribution.

Unlike most informational signals here, this one has a direct test: does entropy mark
the projections that actually miss? On leak-free boards, binning by entropy and
measuring mean absolute percentage error against real season points gives 0.381 /
0.529 / 0.707 across three bins in 2024 (n 356) and 0.366 / 0.510 / 0.704 in 2025
(n 347) — monotonic in both. Past seasons carry no ESPN projection, so that scores the
snap-churn half; the disagreement half is the same signal `role_multiplier` already
prices. `entropy_kind` splits the two uncertainties that human rankings mash together:
ESPN above a model built from past production is unresolved upside, ESPN below it is a
role in doubt. They are not the same bet, and late in a draft the first one is often
underpriced.

## Scoring-format conversion

FantasyPros publishes only full PPR for overall redraft — no half-PPR or standard board
exists upstream. Used unconverted it misprices exactly the players the format is about.

PPR is the baseline; other formats are converted. The market ranking stays the anchor,
because it encodes talent, situation and injury news no model captures. Only the format
delta is applied, and that delta is arithmetic rather than opinion — half PPR is PPR
minus half a point per catch, with reception volume from each player's own projection.

Damped to 0.6, because rooms move less than pure points math: they also price
consistency, scarcity and name recognition, none of which change with scoring.

> Undamped, Derrick Henry went from ADP 38 to 1.0 in standard. Right direction, absurd
> magnitude.

## Rookies

No NFL history, so nothing to regress — but draft capital is a strong predictor, because
it encodes both the league's talent evaluation and the opportunity a team commits to a
player it just spent a high pick on.

Two estimators, because neither is safe alone. A log-linear fit on log(pick) is smooth
and uses every data point but extrapolates badly at the top of the draft. Empirical bin
medians are honest there but noisy. Predictions blend by sample size and **cap at the
bin's observed 75th percentile**, so the model can't promise an outcome nobody has
produced.

Medians rather than means throughout — rookie outcomes are heavily right-skewed.

> The pure fit predicted 19.4 PPG for a back at pick 3. The top-ten bin has actually
> averaged 15.9 across six players in ten years.

Availability scales with draft capital, not the positional average. Consistency is
deliberately low for every rookie: roles move mid-season and the floor is a healthy
scratch.

## Separation

NGS publishes tracking-measured `avg_separation` — yards between receiver and nearest
defender when the ball arrives. Same underlying quantity as PFF's SEP, from chips rather
than human charting.

YPRR and TPRR need routes run, which no free source publishes. Routes are estimated as
snap share × team dropbacks, damped for backs and in-line tight ends who stay in to
block. Validated against a published PFF table for 2025 Indianapolis: TPRR landed within
a few hundredths and the ordering matched.

Qualification is strict — 250 estimated routes and 50 targets. These are rate stats, and
a part-time receiver posts a flattering YPRR that says nothing about a real workload.

**Man/zone splits are not reproducible** from open data. That needs per-play coverage
classification, which only manual charting provides.

## Coverage-scheme trend (opt-in, off by default)

`coverage_trend_weight` (via `model_settings`) is a supplied belief, the same category
as `qb_boost`: it adjusts `draft_score`, but not from a real per-player signal this
project can validate the way `separation` or `td_luck` can. Man-vs-zone and
nickel-vs-base personnel rates aren't in any open dataset — see the man/zone caveat
under Separation above — so there's no equivalent of `matchup_backtest` or
`redzone_shift_backtest` to check it against real outcomes. It exists because external
2025-season analysis (PFF, MatchQuarters, Sharp Football) found defenses shifted hard
toward zone coverage (man coverage down to 22.6% of snaps, from 33%+ seven years prior)
and, later in the season, from nickel back to base personnel (nickel ~68%→61%, base
~23%→29%) once split-safety shells stopped working. Base personnel means a linebacker,
not a nickel corner, more often ends up covering the slot receiver or a back releasing
into the flat — a mismatch that rewards short-area quickness over boundary/vertical
separation.

When enabled, it rewards WR/TE with a short-area profile — high TPRR, low `adot`
(average intended air yards, from the same NGS receiving data `separation` uses) —
over vertical/boundary receivers, and RBs with real receiving role (`target_share`)
directly. Defaults to 0, like `qb_boost`, because it's an opinion about the league
environment rather than something derived from any player's own history. The
underlying rates are season-specific and will decay — re-verify them before trusting a
nonzero value in a future season.

## Team drive efficiency and red zone identity

Two team-level signals surfaced through `team_context`, informational only -- like
`matchup_z` in `separation_report`, neither is folded into `draft_score`.

**Drive efficiency** (`pct_td`/`pct_fg`/`pct_punt`) is the share of a team's drives
ending in each outcome. It doesn't get blended into a player's projection because it's
already baked into his raw points -- a receiver on an efficient offense already scored
more touchdowns last season for exactly that reason. Applying it a second time as a
multiplier would double-count the same information the docstring on `team_context`
already warns about for `team_offense_context`.

**Red zone identity shift** is a team's neutral-field pass rate minus its red zone pass
rate. This one plausibly could have refined confidence in whether a receiver's red zone
role holds up -- a team that goes noticeably run-heavy inside the 20 might not keep
feeding a receiver who racked up season-long volume everywhere else. `redzone_shift_backtest`
tested exactly that, the same way `matchup_backtest` tests schedule difficulty: does
blending the shift into the existing touchdown-luck signal (`m_td_luck`) predict next
season's real points better than the touchdown-luck signal alone? A 2022-2025 run found
it doesn't -- it makes the prediction *worse* for both WR (`improvement_corr` -0.006
across 300 player-seasons) and TE (-0.053 across 117), the same conclusion
`matchup_backtest` already reached for schedule difficulty. It stays informational in
`team_context`, not folded into `m_td_luck` or `draft_score`. Re-run `redzone_shift_backtest`
if the underlying feature or model changes.

## Backtest

`draft_value_history` compares preseason consensus to actual finish across 913 draftable
player-seasons.

Value is measured in **points against what that draft slot actually returned** — "did RB5
capital buy RB5 production?" Rank movement would be unfair to early picks, since
undrafted breakouts push every drafted player down the final standings.

Historical rankings are converted to your format using the *prior* season's points in
both formats — what a drafter knew in August. Using the season's own receptions would
leak the result being measured.

> First attempt rank-recentered everyone and pushed quarterback bust rates to 50%, which
> is nonsense: quarterbacks don't catch passes. They now hold steady at 29–31% across
> all three formats.

Unmatched names are excluded rather than counted as zeros. Without that, Joshua Palmer
showed a 0.00 return across four seasons purely because FantasyPros writes "Josh" — a
fabricated bust sitting in the middle of the results.

## Known limitations

- **Second-year players** sit awkwardly: enough history to leave the rookie curve, not
  enough for the veteran regression to trust.
- **ADP is format- and league-specific.** Consensus is a decent default; your platform's
  export is better.
- **No in-season usage updates.** This is a draft tool.
- **Man/zone coverage splits** are unavailable. `coverage_trend_weight` proxies for
  the trend qualitatively (TPRR/aDOT for WR/TE, target_share for RB) but defaults to
  0 and isn't backtested — see "Coverage-scheme trend" above.
- **Kickers and defenses** aren't modelled. Take them last anyway.
