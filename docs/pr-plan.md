# Splitting feat/espn-live-draft into upstream PRs

Base: `zacharytran26/Fantasy-Football-Draft-MCP` master at `1a68844`.
Branch: `feat/espn-live-draft`, 122 commits, 13 of them merges from the
integration rounds across three parallel branches.

No PR is opened without the user's say-so.

Claims here about what applies cleanly were tested by cherry-picking into a
detached worktree at the upstream base. Claims about what a PR contains were
read off the tree. Where something is asserted rather than measured, it says so.

## The mechanism this branch forces

Themes interleave, because three people worked in parallel and each merge round
touched the same files. `src/ffdraft/server.py` is touched by most commits, the
changelog by nearly all of them, and two commits split across PR boundaries.

Only a five-commit prefix cherry-picks. Everything after it assumes file state
belonging to work assigned elsewhere. So the shape is a **stack**: each PR after
the first two is written as fresh commits on the previous PR's branch, taking
this branch's final state of the files it owns as the target.

### Verified clean prefix

These five cherry-pick in order onto `1a68844` with no conflict, and the
resulting tree passes its own tests. Re-verified after both merge rounds.

    3c2ae0d  justfile
    1f46494  ignore per-machine Claude Code settings
    c93a58f  just ci-matrix
    d750617  paste parser: dotted initials, "Round N, Pick M"
    156496e  merge dotted initials in name keys

Measured in a detached worktree at the base: five clean cherry-picks,
`ruff check src tests` clean, 100 tests passed.

There is no type check to run at this prefix. `just check` here is ruff and
pytest; the `uvx ty check` line arrives with `353be59`, whose hunks are
distributed by file below. Whichever PR takes that hunk takes the shipped form,
not `353be59`'s original:

    uvx ty check --python "{{ venv }}" src tests

The bare `uvx ty check src tests` looks for a `.venv` beside the project and,
finding none, resolves third-party imports against whatever uv cache it lands
on — reporting numpy, pandas and pytest as unresolvable and burying any real
finding under a page of them. A fresh clone and a git worktree both look like
that. The quoting is load-bearing: `justfile_directory()` yields a Windows path
with backslashes, these recipes run through bash, and bash strips a backslash
from an unquoted word, so ty receives `C:Userswilldevespn-ffd-mcp/.venv` and
fails as "cannot find the path specified" — indistinguishable from the missing
environment the flag exists to report. `just -n` cannot show this; it prints
after just's interpolation and before the shell's.

`venv` is the justfile's derived environment: this checkout's `.venv` when it has
one, else the main checkout's, found through `git rev-parse --git-common-dir`. A
worktree has no `.venv` of its own, so before that fallback existed `just check`
could not run in one at all and every agent ran its three steps by hand. The
justfile also exports `PYTHONPATH` to this checkout's `src`, because the venv's
editable install names whichever checkout created it — without that a worktree
borrowing the main checkout's venv tests the main checkout's code.

### Commits that split across PRs

Divide these by hunk; none goes whole into any single PR.

- `3121b47` — next-pickers list for `draft_room` (PR 3) and `draft_strength`
  (PR 5).
- `72a6be6` — role-check smoothing in `model.py` (PR 4) and replay scoring in
  `replay.py` (PR 5).
- `353be59` — a type-check sweep across six modules; its hunks follow whichever
  PR owns the file they land in.

## Order

1 → 2 → 3 → 4 → 5 → 6, with 7, 8 and 9 stacked after 3. Each PR must be green
under `just check` and `just ci-matrix` with only its own stack applied.

## Integration

One person lands on the branch. Owners work in worktrees and hand over a commit;
merges, pushes and the updates below happen in the main checkout, and only ever
from committed content. The main checkout is the live server's source, so an
edit in progress there is read by the next reload.

Three checks on every merge. Each exists because skipping it shipped something.

- **Verify from the blob, not the working tree.** `git show HEAD:<path>`, not the
  file on disk. A merge that auto-committed once left a fix staged and unmerged
  while `just check` passed against the working tree, so a green run and a broken
  commit were reported as the same thing.
- **Replay identity, against a baseline from before the module existed.** Where a
  change touches the model, `just replay` before and after must be identical,
  diffed as whole files rather than read. The baseline has to predate the work:
  comparing against a commit that already contains it proves only that the later
  commits were inert, which is a weaker claim than the one being made.
- **Compare what the consumer reads, not a total over it.** An identity or
  aggregate check is blind to a change that moves a distribution while leaving
  its mean alone, and that is not a rare case: `counting_survival` takes the
  Poisson binomial of the per-pick hazards, not their sum. Measured on the fix
  that stopped `held_by_slot` dropping a malformed row, over one horizon: the
  D/ST taker total moves by 0.008, which any total-only check calls noise, while
  that team's per-pick hazards go from `[0.5, 0.5]` to `[0.008, 1.0]` — smeared,
  against "almost certainly not at his first remaining pick, certainly at his
  last" — and the survival a user reads moves 0.146 to 0.133. The same commit
  moves the K total by a whole taker and its survival by 15 points, so one
  position would have been caught and the other waved through. Ask what function
  consumes the number before choosing what to diff.
- **Every new test confirmed to fail with the change reverted.** A test written
  from the claim it is meant to check will pass on code that never had the
  property. This has caught something in three of the last four merges, including
  a test that could not fail at all.
- **A pass in the main checkout after the merge, not only the author's.** Their
  run is evidence about their tree; this one is evidence about the branch.

A test count from a worktree only means what it says if that worktree imported
its own code. The venv installs the package editable and the resulting path file
names whichever checkout built the venv, so a worktree borrowing another's venv
lints and type-checks its own files — ruff and ty are given paths — while pytest
imports the other tree and runs the worktree's tests against it. Green for code
it never loaded. Measured from a worktree with no venv of its own:

    without PYTHONPATH   ffdraft resolves in the MAIN checkout
    with it              ffdraft resolves in the worktree

The justfile exports `PYTHONPATH` to the checkout's own `src` for this reason,
so every recipe imports the tree it runs in. The 16 `[script]` recipes are not
covered — a just setting is a const context and rejects the derived variable —
and still need a local `.venv`, which matters most for the recipes that produce
evidence, since those and `check` could otherwise read different trees.

The revert check above already answers this without being asked. If breaking
your own file does not break your tests, either the test is tautological or the
suite is not loading your code, and both are worth knowing. Anyone who has run
one has proved their import path; a green run that has never been perturbed has
proved nothing about which tree it read.

That proof is per owner and per change, not a property of the repository. A
revert check says the files it perturbed were loaded from the tree it perturbed
them in, and says nothing about a test file nobody has broken on purpose. It is
the cheap check, and it expires. `tests/test_import_path.py` is the standing
one: it asserts the imported package sits under the checkout the tests were
collected from, and that no already-imported submodule comes from a different
root, so the silent configuration — both paths importable, the wrong one
winning, everything green — fails loudly instead. Do not read "we have the
revert check" as covering what that test exists for.

### The one defect this codebase keeps making

`NaN` is truthy, and pandas hands it back wherever a value is absent. Every
idiom that treats "missing" as "falsy" is therefore wrong here, and reads as
obviously correct. Three separate instances surfaced in a single integration
session, in three modules, written by three people:

    x or fallback              the fallback is never reached; NaN wins
    bool(row.get(k, False))    a row with no value reports True
    if row.get(k):             a missing value takes the branch

What each one shipped, in the tool's own voice: a defense whose injury line read
"ESPN status nan"; a board-priced player labelled a replacement-level stand-in
with the user told his number is not real; and a roster reported as `"NaN": 1`,
which then raised `TypeError: '<' not supported between instances of 'str' and
'float'` when the tool sorted its own output.

The remedy is the same each time and it is not a style choice. Ask the question
you mean: `pd.notna(x)` for presence, `x is None` for absence of a key, and
`bool(pd.notna(x) and x)` for a flag read out of a frame. Never let truthiness
stand in for any of them. On review, the grep is cheap and worth running over
any diff that reads a frame: `\bor\b.*get\(|bool\(.*get\(`.

**Not `x is True`.** That was written here first and it is wrong on the path
that matters. A real bool column hands back `np.True_`, which is not the Python
singleton, so `is True` is `False` for a flag that is genuinely set — it stops
firing on every frame that carries the column on every row, which is the normal
case, and keeps working only on the mixed-source frame the defect was found in.
It therefore fails silently in exactly the direction a reviewer will not look.
Measured on pandas 3.0.5 / numpy 2.5.2 across all six states — bool column
True/False, object column with a Python `True`, object column with NaN, the
concat that leaves NaN in the row lacking the flag, and no column at all:
`bool(pd.notna(x) and x)` is right in all six, `is True` in four.

Serialisation is the same hazard wearing different clothes: `json.dumps` writes
a float NaN as a bare `NaN` literal, which Python reads back and every other
client rejects. That is why every tool payload leaves through one sanitising
emit, with a static test that fails if a handler calls `json.dumps` directly.

Keeping this document current is part of the merge, not a pass at the end. It
went stale once by 46 commits, and the worst of it was a note telling the reader
to work around a defect that had since been fixed.

- A module with its own files and its own tests becomes its own section here,
  added when it lands, with its sources, its tests, and its negative results.
- A change to shared surface — `model.py`, `server.py`, the board or the market
  join — joins the section that already owns that file. It does not get a new
  one, or the stack stops being reviewable.
- Say which of the two a change is when it lands, rather than leaving it to be
  inferred later from the file list.

"Shared surface" means the inputs as well as the code. `waivers.py` reads
`roles.bench_values`, so the change that could break it is not one that edits
`roles` but one that alters what reaches it — `#40` changes what `my_rows`
hands over without touching `roles` at all. "`roles` is unchanged" and "what
reaches `roles` is unchanged" are different claims, and only the first is
settled by a file list. When a merge changes what a shared function is fed, tell
the owners who read that function, because their fixtures build their own frames
and will not notice.

## PR 1 — Tooling: justfile and the CI matrix

Cherry-pick `3c2ae0d`, `1f46494`, `c93a58f`. Verified clean on the base.

A `justfile` covering setup, check, data, serve and smoke, and `just ci-matrix`,
which runs upstream's CI locally on 3.10, 3.11 and 3.12 plus the build. Ignore
rules for `.mcp.json` and per-machine Claude Code settings.

Tests: none of its own. It is the harness every later PR's green claim rests on,
which is why it goes first.

Two commits that look like tooling are not: `51f4e6a` needs the dump-directory
ignore from PR 3, and `b797171` documents ESPN draft history in a file PR 3
creates. Both conflict on this base and belong to PR 3.

## PR 2 — Name keys

Cherry-pick `d750617`, `156496e`. Verified clean on PR 1.

The paste parser dropped names with dotted initials, which shifted every later
pick, and mangled "Round N, Pick M - Name" because the comma split ran before
the prefix strip. Name keys now merge dotted initials, so "D.J. Moore" and
"DJ Moore" resolve to one player.

Tests: `tests/test_names.py`, `tests/test_board.py`.

Self-contained and useful to upstream even if nothing else lands.

## PR 3 — ESPN live draft: socket, watch, picks, queue, room, dump

Stack. Sources: `e2072b9`, `0de2957`, `9c849c8`, `3b91833`, `e6a829d`,
`cb64810`, `caf3318`, `9464bdc`, `a6e90aa`, `1e77d69`, `a516da1`, `498cb53`,
`51f4e6a`, `b797171`, and the `draft_room` hunks of `3121b47`.

ESPN's read API returns no picks until a draft completes, so `sync_draft` joins
the draft-room socket, decodes the INIT snapshot and returns every pick against
the team's real slot. `watch_draft` holds the socket open and pushes each pick
into the session; `make_pick` and `set_draft_queue` write back over it;
`draft_room` reports presence and chat. ESPN allows one draft-room connection
per team, so the watch pauses when the browser room opens rather than fighting
it, and `sync_draft` refuses while a watch is connected. `dump_draft` writes
every read-API view, the raw INIT payload and the socket log to disk.

Tests: `tests/test_espn_live.py`, `tests/test_watch.py`,
`tests/test_espn_dump.py`, fixture `tests/fixtures/espn_draft_init.b64`.

The largest of the nine. If upstream wants it smaller, the seam is `e2072b9`
and `0de2957` (socket decode and in-progress sync) as their own PR, with the
watch and the socket-backed tools on top. It needs credentials and a team the
user owns, so it cannot run in upstream CI; the tests use the recorded fixture.

## PR 4 — ESPN ADP, projections, league rules, byes, K and D/ST

Stack. Sources: `c745e37`, `5eb2fde`, `6e3e87c`, `b2c8362`, `88df98c`,
`9f99928`, `48f7741`, `a6846f5`, `8528c6c`, `df2a0eb`, `d7807db`, and the
`model.py` hunks of `72a6be6`.

Survival odds are priced off ESPN's own average draft position rather than
consensus. ESPN's season projection becomes a role check, scaling `pick_value`
where ESPN and the model disagree, which caught a model RB22 that ESPN had as a
backup. Kickers and defenses reach the board at all for the first time, priced
from ESPN's list rather than guessed, with their own replacement levels.
`league_rules` reads the league's real settings instead of an assumed template.
Every row carries `bye_week`. ESPN's placeholder ADP is treated as undrafted
rather than as pick 170, and the survival model gains an exponential right tail.

Tests: `tests/test_board.py`, `tests/test_bye.py`, `tests/test_model.py`.

Two negative results belong in the description because they are why defaults
ship as they do: the bye-stacking penalty stays at 0, and the survival-tail
aggregates were reported as proving nothing rather than as support.

Take the bye figures from CHANGELOG.md when the description is written, and do
not restate them here. This paragraph quoted -2.1 weekly points over 2022-2025
long after two re-runs had replaced it, most recently with three of the four
seasons showing their two blocks pointing opposite ways by 8.0, 10.5 and 31.8.
The conclusion never moved; every magnitude behind it did, twice. A number
copied into a second place is a number that will disagree with the first, and
this one is measured by a harness that changes whenever `recommend` does.

## PR 5 — Replay, room drift, choice model, counterfactual, snapshots

Stack. Sources: `4232fac`, `054a38f`, `9b90e2b`, `11fc62f`, `747db8b`,
`ae3650d`, `7d3168c`, `adaf7bd`, `e575c00`, `d902e1a`, `7ce6c1a`, the
`draft_strength` hunks of `3121b47`, and the `replay.py` hunks of `72a6be6`.

Every pick is re-run through the model for the team on the clock. `room_drift`
measures how many picks before ADP the room drafts and feeds a shift back into
the recommender; over 1,060 live survival forecasts the per-position shift moved
Brier from 0.140 to 0.128 against a 0.250 base rate. `choice.py` fits four
conditional-logit predictors scored out of sample, and `predict_pick` applies
them to another team's roster. `draft_counterfactual` runs the model as one
team with a control arm, so the reported delta is the intervention rather than
the difference between the predictor's room and the real one. As-of snapshots
record the market at every pick so a replay is not scored against today's
numbers.

Tests: `tests/test_replay.py`, `tests/test_choice.py`.

Depends on PR 4 for `espn_rank` and the ADP columns. Team-specific effects were
measured and left off by default; keep that in the description.

## PR 6 — Board-key freshness, market join, draft audit

Stack. Sources: `a343ae3`, `72b954a`, `6425798`, `765938f`, `d70f291`,
`4dd0b59`.

The other half of the name-key theme, here rather than with PR 2 because every
commit in it needs `server.py` and `watch.py` from PR 3.

Accents fold in name keys, boards carry a key version, and a board written by an
older normaliser is re-joined on load. `attach_adp` joins through the alias
index after the exact key, at the same position only, and records how each row
joined. `draft_audit` checks the invariants between board, draft state and
recommendation and reports what stayed unpriced.

Tests: `tests/test_board.py`, `tests/test_names.py`.

Written after two live incidents: dotted-initial names keyed differently on the
board and in the draft state, and a cached board keeping stale keys. Both belong
in the description as the motivation.

## PR 7 — Draft room report

Stack. Sources: `c479f39`, `7a1df03`, plus `src/ffdraft/roomstats.py` and
`tests/test_roomstats.py` whole.

Who was in the ESPN draft room, for how long, and who talked: minutes present,
joins and leaves, messages, busiest hours, picks made and the seconds each took.
Reads the running watch when there is one, else a dump directory. SWIDs are
never reported.

Tests: `tests/test_roomstats.py`.

Depends on PR 3 for the watch. Self-contained otherwise, and the smallest of the
feature PRs.

## PR 8 — Role features

Stack. Sources: `918436c`, `1a24cae`, `d42cc8d`, `b0847d1` if it lands, plus
`src/ffdraft/roles.py` and `tests/test_roles.py` whole.

Start probability, handcuff value, opportunity share and role entropy, priced
and surfaced. Both weights ship at 0, on the paired-draft evidence recorded with
them.

Tests: `tests/test_roles.py`, `tests/test_blocks.py`.

Depends on PR 4. The default of 0 with the evidence beside it is the point of
the PR, not a placeholder.

## PR 9 — Hot reload and integration hygiene

Stack. Sources: `e7cf48a`, `e208f75`, `c48eaa9`, `e67a827`, `31a5e91`,
`e461b00`, `1b89916`, `dde6f08`, `96e98e5`.

Lands last, because `96e98e5` rewrites all 83 `json.dumps` call sites in
`server.py` and so needs every tool the earlier PRs add.

`reload_code` re-imports the package and refreshes the served tool registry
without a reconnect, so a code change does not drop a live watch. It works under
`python -m ffdraft.server` as well: that launch registers the module as
`__main__` only, leaving `ffdraft.server` absent from `sys.modules` while
`__spec__.name` still says `ffdraft.server`, and the reload re-registers it
under the spec name rather than failing on the gap. Two tests keep the mechanism
honest: one asserts the reload order lists every module in the package, in both
directions; the other asserts no module defines the same top-level name twice.

Every tool payload also goes out through one sanitising emit. `json.dumps`
writes a float NaN as a bare `NaN` literal, which Python's own parser reads back
and every conforming client rejects, so the failure is invisible from inside the
process and total from outside. A static test fails if any handler calls
`json.dumps` outside `_emit`, because the round-trip test can only reach tools
that run without a network.

Tests: `tests/test_reload.py`, `tests/test_no_duplicate_definitions.py`,
`tests/test_json_payloads.py`.

The second test is not incidental. During integration, two branches fixed one
defect independently and git merged both definitions with no conflict, leaving
the earlier unbounded form live and the fix dead above it. It happened twice.
Upstream will hit the same shape the first time two contributors fix one bug in
parallel.

## PR 10 — Waiver targets from role change

Stack. Sources: `805c4f5`, `1fc0acf`, `0bbd699`, `8f328a7`, `a7b0a0e`, and the
wiring commit, as landed.

A new `waivers.py` scores a free-agent claim on the role moving rather than on
the points scored, with `roles.handcuff_table` and `roles.bench_values` behind
it. `server.py` adds `_waiver_inputs` — the only part that touches the network
or the caches, so a test replaces it whole — and the `waiver_targets` tool,
which goes out through `_emit` like every other payload; `just waivers <week>`
prints the ranked table. The round trip is covered by a control that turns
sanitising off and requires the break, so it cannot go vacuous if the fixture
stops carrying a hole: today that hole is `handcuff_for`, filled by `.map` and
therefore NaN on every claim that is not a handcuff. The claim list says which kind of empty it is when
it is empty, the contingency is resolved before the cut rather than after it —
a handcuff's `role_change` is 0 by construction, so cutting first dropped him
before his contingency was read — and a drop says when it rests on a
replacement-level stand-in rather than on a projection.

Tests: `tests/test_waivers.py`. Surfaces in `docs/data-sources.md`.

Two settings traps belong in the description, both measured off a real dump
rather than assumed: `isUsingAcquisitionBudget` is false while
`acquisitionBudget` and `minimumBid` are populated and inert, so a tool that
reads the budget first recommends bids to a league that does not take them; and
`isBenchUnlimited` is true while six bench slots exist, so every claim names a
drop. Undroppable players come from `player.droppable` in a view already
captured, 19 of 1036 rows on that dump.

Three of the four scores carry `unmeasured` in every row and the free-agent pool
carries `unverified-shape`, because the capture was taken mid-draft and reports
every player as a free agent, so the split the tool selects on has never been
exercised. Those labels ship; they are not placeholders to quietly drop.

Found on review by marge and landed with the wiring: `drop_candidate` read
`bool(worst.get("unpriced", False))`, and NaN is truthy. A bench assembled from
two sources where only one carries the column concatenates to dtype object
holding `[False, nan]`, and the row with no flag reported `unpriced` True —
labelling a board-priced player a replacement-level stand-in and telling the
user his number is not real when it is. It was unreachable while every frame out
of `my_rows` carried the column on every row, and became reachable at the wiring.

The fix is `bool(pd.notna(worst.get("unpriced")) and worst.get("unpriced"))`,
**not** the `is True` this document recommended until it was measured; see the
section above for the six states and why `is True` fails on the ordinary frame
rather than the odd one. Regression test:
`test_a_bench_from_mixed_sources_does_not_invent_a_stand_in`.

## PR 11 — Weekly lineup and live ESPN rosters

Stack. New `lineup.py` and `rosters.py` with their tests.

`lineup.starting_lineup` answers who starts from the league's own slots rather
than from board rank, and `lineup.droppable` is its complement. That distinction
is not cosmetic: the tool that reached for "everyone outside my top N by rank"
offered the user's only defense as a drop while the same row reported it starting
every week, because a receiver-heavy roster puts the kicker and defense in the
tail of a rank ordering. `rosters.read_rosters` pulls a week's ESPN rosters into
the board's row shape, through the same replacement-level stand-in the draft
record uses, so a player the board cannot price holds his slot instead of
vanishing.

Tests: `tests/test_lineup.py`, `tests/test_rosters.py`.

Two shared-surface additions belong with it. `board.is_position` asks whether a
value *is* a position — a non-empty string — rather than whether it is present;
`pd.notna` is the wrong test here because it passes `0.0` and `""`. And
`board.espn_cookies` / `board.espn_league_url` are the cookie jar and league URL
written once. They were forked six ways beforehand, five in `board.py` and one in
`adp.py`, each independently remembering that ESPN rejects a bare SWID. The six
existing copies were deliberately left alone rather than rewritten mid-integration;
converging them is its own pass, recorded under Open.

## PR 12 — Kicker and defense streaming

Stack. New `stream.py` with its tests, and `stream_kdst`.

Weekly K and D/ST ranked by matchup, with a look-ahead, and calibrated where the
data supports it. Every margin carries the unit it is honestly in: `adp.margin_unit`
decides whether a number may be called points or only an ordinal ranking, and
this module asks rather than asserting.

Tests: `tests/test_stream.py`.

Known before it ships: the payload is 63 KB for one week, of which the two ranked
lists are 16 KB per week over two weeks at roughly 266 bytes a row. Compact JSON
is 39 KB, so indentation is a third of it. A client can refuse a response that
size. The cap belongs on the ranked lists; the calibration blocks are not the
problem.

## PR 13 — Trade evaluator

Stack. New `trade.py` with its tests, and `evaluate_trade`.

Both rosters simulated week by week on their own starting lineups over the
scored window, with byes and injury availability, reported as points before and
after with the spread between disjoint seed blocks beside every estimate. A side
whose blocks disagree in sign is reported as no call rather than as a win.

Tests: `tests/test_trade.py`.

The calibration rule it declares itself under is worth the description: a harness
that fitted nothing may report points if it says so and declares the unit its
inputs already carry, and `adp.HARNESS_FITTED` is the strict default. That is
narrower than "the output is in points", because a replication's inputs may
themselves come from something fitted.

Open against it: `resolve` looks names up against the raw board, so a roster
holding a player the board cannot price refuses the whole trade — including when
that player is not in it. The stand-in that every other roster path gets does not
reach here. Refusing on an unpriced `give` or `get` is right and the docstring
gives the right reason; refusing on a bystander is not.

## Open

- Shared surface, joining the section that owns the file rather than getting one:
  `draft_retrospective` extends `replay.py`, so it belongs to PR 5; the pick
  queue's merge semantics extend `watch.py` and `server.py`, so they belong to
  PR 3. Both are named here so the file list is not the only record of the
  decision.
- The cookie jar and league URL are constructed six times outside
  `board.espn_cookies` and `board.espn_league_url` — five in `board.py`, one in
  `adp.py`. Each remembers independently that ESPN rejects a bare SWID, which is
  the `_discount` fork in its early state: one rule written six times and then
  corrected once. The new code routes through the shared pair; converging the
  six is a quiet pass of its own, not something to fold into a feature merge.
- `353be59` cleared every type-check finding across the package. Its hunks are
  distributed by file above. If upstream would rather take the sweep whole, it
  becomes a tenth PR that lands last, and it carries the `just check` ty line in
  the form given under the clean prefix.
- The three tasks open when this was written have landed: the plan's ADP cut for
  kickers and defenses and the every-turn count of a required position in PR 4,
  exclude semantics for a zero start probability in PR 8, and the two-block bye
  re-run in PR 4.
- Checked by walking the 41 files the branch touches against the assignments
  above. Every source and test file is reached by commits some PR owns. The
  shared ones carry no commit of their own — `adp.py`, `board.py`, `config.py`
  and `features.py` move with whichever feature commit touched them, as do
  `README.md`, `SECURITY.md`, `pyproject.toml`, `docs/data-sources.md` and
  `docs/methodology.md`.
- `docs/tools.md` is touched by 46 commits and will conflict in every stacked
  PR. Take this branch's final text for the tools that PR ships, and nothing
  else from it.
- `CHANGELOG.md` is touched by nearly every commit. It is the worst conflict on
  the branch and carries no code; write each PR's entry fresh rather than
  porting hunks.
- This file ships in no PR.
- Three results about one question belong in PR 4's description, because
  together they are why `_absorbed_by`, `_plan_pool` and the K/D-ST survival
  term ship as they do. Replacing even absorption with a curve fitted to the
  room's observed timing makes the plan worse, taking a defense in round 9 of 14
  (#33); making pool membership and survival agree by counting makes it worse by
  the same mechanism, because a count is an expectation and a step function
  treats it as fact (#35); and the survival term itself was miscalibrated for
  defenses, which #37 fixes by counting the room's own record per board index.
- Say in the description that three separate changes to K/D-ST survival each
  tried to move the same pick into round 9, and that the third is the one that
  did not. The pattern is the argument for the shape #37 settled on, and a
  reviewer who sees only the third change will not know the first two were
  measured and rejected.
- `5874482` ignores `.claude/worktrees/`. It is an artifact of how this branch
  was built and ships in no PR.
