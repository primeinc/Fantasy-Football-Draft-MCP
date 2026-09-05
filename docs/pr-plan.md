# Splitting feat/espn-live-draft into upstream PRs

Base: `zacharytran26/Fantasy-Football-Draft-MCP` master at `1a68844`.
Branch: `feat/espn-live-draft`, 76 commits, 7 of them merges from two
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
`ruff check src tests` clean, 100 tests passed. `ty` was **not** verified there:
every finding was an unresolved import for numpy, pandas and requests, which is
a bare worktree having no environment rather than a defect. Create the venv on
the PR branch before claiming the type checker.

### Commits that split across PRs

Divide these by hunk; neither goes whole into any single PR.

- `3121b47` — next-pickers list for `draft_room` (PR 3) and `draft_strength`
  (PR 5).
- `72a6be6` — role-check smoothing in `model.py` (PR 4) and replay scoring in
  `replay.py` (PR 5).
- `353be59` — a type-check sweep across six modules; its hunks follow whichever
  PR owns the file they land in.

## Order

1 → 2 → 3 → 4 → 5 → 6, with 7, 8 and 9 stacked after 3. Each PR must be green
under `just check` and `just ci-matrix` with only its own stack applied.

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
ship as they do: the bye-stacking penalty measured -2.1 weekly points over
2022-2025 and stays at 0, and the survival-tail aggregates were reported as
proving nothing rather than as support.

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

Stack. Sources: the `reload_code` commit, the reload-order completeness test,
and the duplicate-definition test.

`reload_code` re-imports the package and refreshes the served tool registry
without a reconnect, so a code change does not drop a live watch. Two tests keep
the mechanism honest: one asserts the reload order lists every module in the
package, in both directions; the other asserts no module defines the same
top-level name twice.

Tests: `tests/test_reload.py`, `tests/test_no_duplicate_definitions.py`.

The second test is not incidental. During integration, two branches fixed one
defect independently and git merged both definitions with no conflict, leaving
the earlier unbounded form live and the fix dead above it. It happened twice.
Upstream will hit the same shape the first time two contributors fix one bug in
parallel.

## Open

- `353be59` cleared every type-check finding across the package. Its hunks are
  distributed by file above. If upstream would rather take the sweep whole, it
  becomes a tenth PR that lands last.
- Three tasks were still open on the branch when this was written: the plan's
  ADP cut for kickers and defenses, exclude semantics for a zero start
  probability, and a seed-block re-run of the bye weight. None is required by
  any PR above; each lands wherever its file already sits.
