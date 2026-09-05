set minimum-version := '1.55.0'
set default-list

# The virtualenv the recipes run from: this checkout's own `.venv`, else the main
# checkout's, reached through `--git-common-dir` (whose parent is the main
# checkout). A git worktree never has one -- `.venv` is untracked and
# `git worktree add` copies nothing -- so without the fallback every recipe here
# resolves a python that is not on disk, and `check` failed before it ran.
#
# The git call sits in the else branch deliberately. just evaluates variables
# eagerly on every recipe invocation, but does not evaluate the untaken branch of
# an `if`, so a checkout that has its own `.venv` never shells out. `|| true`
# keeps a directory that is not a repo from aborting just with git's error before
# `check` can name the problem itself.
#
# `just -n` does not evaluate `shell()`: a dry run prints this expression's source
# text and a path built from it, which is nonsense and not what runs.
# `just --evaluate venv` shows the value the recipes actually get.
_local := justfile_directory() / '.venv'
_common_dir := if path_exists(_local) == 'true' { '' } else { shell('git -C "$1" rev-parse --path-format=absolute --git-common-dir || true', justfile_directory()) }
venv := if path_exists(_local) == 'true' { _local } else if _common_dir != '' { parent_directory(_common_dir) / '.venv' } else { '' }
python := venv / 'Scripts' / 'python.exe'

# `[script]` recipes need the same venv, and a setting cannot name `venv`
# directly: a setting is a const context and rejects a derived variable ("cannot
# access non-const variable `python` in const context"). So the interpreter asks
# for it at run time instead of restating the rule -- `just --evaluate venv` is
# the same single definition above, read back out, and there is still only one
# place that decides where the virtualenv is.
#
# just writes the recipe body to a temporary file and passes its path as an
# argument to this command (README: "run by passing its path as an argument to
# COMMAND"), so under `sh -c SCRIPT` the body arrives as `$0` and is handed
# straight to python. Recipe parameters are exported, not positional, so `$@` is
# ordinarily empty and is forwarded for the case where it is not.
#
# The named line matters more here than in `check`: without it a worktree with no
# venv fails as "The system cannot find the path specified", which names nothing
# and arrives before the recipe's first line.
set script-interpreter := ['sh', '-euc', 'v="$(just --evaluate venv)"; if [ -z "$v" ] || [ ! -x "$v/Scripts/python.exe" ]; then echo "just: no virtualenv python for this checkout, and none in the main checkout. Run: just setup" >&2; exit 1; fi; exec "$v/Scripts/python.exe" "$0" "$@"']

# Every recipe imports THIS checkout's source. The venv installs the package
# editable, and that `.pth` names whichever checkout created the venv -- so a
# worktree borrowing the main checkout's venv would otherwise run and test the
# main checkout's code under its own tests. Exported once here rather than
# prefixed onto individual lines, so no recipe can be added without it.
export PYTHONPATH := justfile_directory() / 'src'

# Create .venv and install the package with dev extras
#
# The path is literal, not `{{ python }}`: setup builds the venv for THIS
# directory, and in a worktree `python` points at the main checkout's.
setup:
    uv venv .venv --python 3.12
    uv pip install --python .venv/Scripts/python.exe -e ".[dev]"

# Lint, type-check, and run the offline test suite
#
# ty is given the environment explicitly. Left to itself it looks for a `.venv`
# beside the project and, not finding one, resolves third-party imports against
# whatever uv cache it lands on -- which reports numpy, pandas and pytest as
# unresolvable and buries any real finding under a page of them. That is what
# happens in a git worktree, where `.venv` is untracked and absent. Pointed at a
# path, ty either uses it or says in one line that it is not there.
#
# Every interpolated path is QUOTED. `justfile_directory()` yields a Windows path
# with backslashes, this justfile runs recipes through bash, and bash eats a
# backslash in an unquoted word: `C:\Users\will\dev\espn-ffd-mcp/.venv` reaches
# ty as `C:Userswilldevespn-ffd-mcp/.venv`, which fails as "cannot find the
# path specified" and reads like a missing venv rather than a mangled argument.
# `just -n` shows the pre-shell text and so cannot show this; only running it can.
#
# The first line names a missing venv once, in a sentence, rather than letting
# three commands each fail their own way against a path that is not there.
#
# pytest imports through the exported PYTHONPATH above, not the venv's editable
# `.pth`; without it a worktree borrowing the main checkout's venv would test the
# main checkout's code and report green for code it never loaded.
check:
    @test -x "{{ python }}" || { echo 'just check: no virtualenv python. Looked for "{{ _local }}", then for a .venv beside the main checkout that git reports. Run `just setup`.' >&2; exit 1; }
    "{{ python }}" -m ruff check src tests
    uvx ty check --python "{{ venv }}" src tests
    "{{ python }}" -m pytest tests -q

# Upstream CI locally: ruff and the test suite on every Python it tests, each
# in a throwaway venv, then the distribution build.
[script]
ci-matrix:
    import os
    import subprocess
    import tempfile

    root = tempfile.mkdtemp(prefix="ffd-ci-")
    for version in ("3.10", "3.11", "3.12"):
        venv = os.path.join(root, version)
        py = os.path.join(venv, "Scripts", "python.exe")
        print(f"== python {version}", flush=True)
        subprocess.run(["uv", "venv", venv, "--python", version, "-q"], check=True)
        subprocess.run(["uv", "pip", "install", "--python", py, "-q", "-e", ".[dev]"], check=True)
        subprocess.run([py, "-m", "ruff", "check", "src", "tests"], check=True)
        subprocess.run([py, "-m", "pytest", "tests", "-q"], check=True)
    here = os.path.join(os.getcwd(), ".venv", "Scripts", "python.exe")
    subprocess.run([here, "-m", "build", "--outdir", os.path.join(root, "dist")],
                   check=True, capture_output=True)
    print(f"build ok -> {os.path.join(root, 'dist')}")

# One-time nflverse download and board build (cache in ~/.ffdraft)
data:
    "{{ python }}" setup_data.py

# Run the MCP server on stdio (what Claude launches)
serve:
    "{{ python }}" -m ffdraft.server

# Standalone draft watch for one ESPN league: keeps the pick state current and
# logs every event to ~/.ffdraft/state/watch_<league>.log without Claude attached.
# Cookies come from .mcp.json. Ctrl+C stops it. Pauses if you open the draft room.
[script]
watch $league_id:
    import asyncio
    import datetime
    import json
    import os
    import sys

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    env = json.load(open(".mcp.json"))["mcpServers"]["fantasy-draft"]["env"]
    os.environ.update(env)
    from ffdraft import board as bd
    from ffdraft import server, watch
    from ffdraft.config import CURRENT_SEASON, STATE_DIR

    league_id = os.environ["league_id"]
    season = int(os.environ.get("FFDRAFT_SEASON", CURRENT_SEASON))
    swid, s2 = env["ESPN_SWID"], env["ESPN_S2"]
    info = bd.espn_league_context(league_id, season, swid, s2)
    if info["my_team_id"] is None:
        sys.exit("no team owned by ESPN_SWID in this league")
    league, weights = server._settings()
    log_path = STATE_DIR / f"watch_{league_id}.log"

    async def notify(content, meta):
        line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {meta.get('event', '')}: {content}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8", newline="") as fh:
            fh.write(line + "\n")

    w = watch.DraftWatch(league_id, season, int(info["my_team_id"]), swid, s2, league,
                         server._build_board(), notify,
                         directory=bd.espn_league_directory(league_id, season, swid, s2),
                         bye_weight=weights.bye,
                         refresh=lambda: (server._build_board(), server._settings()[1].bye))
    print(f"watching league {league_id} as team {info['my_team_id']} (slot {info['draft_slot']}); "
          f"log {log_path}", flush=True)
    try:
        asyncio.run(w.run())
    except KeyboardInterrupt:
        print("stopped", flush=True)

# Replay the recorded draft through the model: per-team totals, calibration of
# the survival odds, biggest reaches and values. Same numbers as the draft_replay
# tool, without a server. $picks limits the per-pick rows printed (0 = none).
[script]
replay $picks='0':
    import json
    import os
    import sys

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    env = json.load(open(".mcp.json"))["mcpServers"]["fantasy-draft"]["env"]
    os.environ.update(env)
    from ffdraft import replay, server

    league, _ = server._settings()
    b, st = server._build_board(), server._state()
    drift = replay.room_drift(b, st)
    print(f"room drift: median {drift['median_reach']} picks before ADP (mean {drift['mean_reach']}, n {drift['n']})")
    print("  by position: " + ", ".join(f"{p} {d['median']} (n {d['n']})" for p, d in drift["by_position"].items()))
    for label, shift in (("none", 0.0), ("room median", drift["median_reach"]), ("per position", drift["shift"])):
        out = replay.replay_draft(b, st, league, adp_shift=shift)
        o = out["overall"]
        print(f"== adp_shift {label}: brier {o['survival_brier']}  baseline {o['survival_brier_baseline']}  log loss {o['survival_log_loss']}")
        for c in o["survival_calibration"]:
            print(f"  p {c['p_range']}  n {c['n']:>4}  predicted {c['predicted']:.2f}  observed {c['observed']:.2f}")
    print(f"picks scored {out['picks_scored']}  on board {o['on_board_picks']}  off board {o['off_board_picks']}")
    print(f"model match rate {o['model_match_rate']}  top-3 rate {o['top3_rate']}  median rank {o['median_rank']}")
    pr = out["predictors"]
    print(f"walk-forward predictors ({pr['picks_scored']} picks scored out of sample"
          + (f"; {pr['picks_unscored']} not priced by the board: {pr['unscored_picks']}"
             if pr["picks_unscored"] else "") + ")")
    if pr["picks_unscored"]:
        print("  these log losses are over the scored picks only -- comparing two runs that "
              "scored different picks is not a comparison")
    for name, s in pr["predictors"].items():
        print(f"  {name:<10} log loss {s['log_loss']!s:>6}  top1 {s['top1']!s:>6}  top3 {s['top3']!s:>6}  "
              f"top5 {s['top5']!s:>6}  median rank {s['median_rank']}")
    fc = out.get("forecast")
    if fc:
        print(f"forecast for pick {fc['pick']} (slot {fc['slot']}, next {fc['next_pick']})")
        print("  position: " + ", ".join(f"{k} {v:.0%}" for k, v in fc["position_probabilities"].items()))
        for name in ("blend", "espn_list", "model"):
            print(f"  {name:<10} " + "; ".join(f"{c['player']} {c['p']:.0%}" for c in fc[name]))
        print("  blend weights: " + ", ".join(f"{k} {v:+.2f}" for k, v in fc["weights"]["blend"].items()))
    print("teams (least projected points left on the table first)")
    for t in out["teams"]:
        me = " <- you" if t["mine"] else ""
        print(f"  slot {t['slot']:>2}  picks {t['picks']}  top3 {t['top3']}  pct {t['mean_choice_percentile']!s:>5}  "
              f"regret {t['pick_regret']:>7}  pts left {t['proj_left_on_table']:>7}  z {t['mean_market_z']!s:>5}  "
              f"need {t['mean_need_mult']!s:>4}  p_next {t['mean_urgency_waste']!s:>4}  off {t['off_board']}{me}")
    print("survival by round (brier / baseline / log loss)")
    for r in o["survival_by_round"]:
        print(f"  round {r['round']:>2}  n {r['n']:>4}  {r['brier']:.3f} / {r['brier_baseline']:.3f} / {r['log_loss']:.3f}  "
              f"predicted {r['predicted']:.2f} observed {r['observed']:.2f}")
    print("survival by position")
    for r in o["survival_by_position"]:
        print(f"  {r['position']:<4} n {r['n']:>4}  {r['brier']:.3f} / {r['brier_baseline']:.3f} / {r['log_loss']:.3f}  "
              f"predicted {r['predicted']:.2f} observed {r['observed']:.2f}")
    print("biggest reaches (market z = (ADP - pick) / ADP spread)")
    for r in o["biggest_reaches"]:
        print(f"  pick {r['pick']:>3} slot {r['slot']:>2} {r['actual']:<28} z {r['market_z']:>6}  reach {r['reach']:>6}")
    print("biggest values")
    for r in o["biggest_values"]:
        print(f"  pick {r['pick']:>3} slot {r['slot']:>2} {r['actual']:<28} z {r['market_z']:>6}  reach {r['reach']:>6}")
    print("biggest regrets (model pick_value left on the table)")
    for r in o["biggest_regrets"]:
        print(f"  pick {r['pick']:>3} slot {r['slot']:>2} {r['actual']:<26} over {r['model_pick']:<24} {r['pick_regret']:>7}")
    n = int(os.environ["picks"])
    for r in out["picks"][-n:] if n else []:
        print(f"  {r['pick']:>3} r{r['round']:<2} slot {r['slot']:>2} {r['actual']:<26} rank {r['actual_rank']!s:>4} "
              f"pct {r['choice_percentile']!s:>5} proj {r['actual_proj']!s:>6} espn {r['actual_espn_proj']!s:>6} "
              f"role {r['role_mult']!s:>4} model {r['model_pick']!s:<22} regret {r['pick_regret']!s:>6} "
              f"z {r['market_z']!s:>5}")

# Replay the recorded draft priced from the market snapshots the watch wrote at
# each pick, against the same replay priced from today's board. Prints the
# coverage first: with no snapshots the two runs are identical and it says so.
# $league_id defaults to ESPN_LEAGUE_ID from .mcp.json.
[script]
asof $league_id='':
    import json
    import os
    import sys

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    env = json.load(open(".mcp.json"))["mcpServers"]["fantasy-draft"]["env"]
    os.environ.update(env)
    from ffdraft import replay, server, watch

    league_id = os.environ["league_id"] or os.environ.get("ESPN_LEAGUE_ID", "")
    if not league_id:
        sys.exit("no league id: pass one or set ESPN_LEAGUE_ID in .mcp.json")
    league, _ = server._settings()
    b, st = server._build_board(), server._state()
    root = watch.snapshot_dir(league_id)
    shift = replay.room_drift(b, st)["shift"]
    aged = replay.replay_draft(b, st, league, adp_shift=shift, as_of=True, snapshots=root)
    c = aged["as_of"]
    print(f"snapshots {c['snapshots']}")
    print(f"  picks {c['picks']}  with a snapshot {c['picks_with_snapshot']} ({c['coverage']})  "
          f"first {c['first_pick_with_snapshot']}  last {c['last_pick_with_snapshot']}")
    print(f"  mean share of the pool each snapshot reached: {c['mean_pool_share']}  "
          f"the player actually taken was in it {c['actual_pick_covered']} times")
    if c["picks_without_snapshot"]:
        print(f"  no snapshot (first 20): {c['picks_without_snapshot']}")
    if not c["picks_with_snapshot"]:
        print("  nothing was recorded during this draft: the numbers below are today's board, "
              "identical to `just replay`. Run the watch through a draft to fill them.")
    today = replay.replay_draft(b, st, league, adp_shift=shift)
    for label, out in (("as of the pick", aged), ("today's board", today)):
        o = out["overall"]
        print(f"== {label}: brier {o['survival_brier']}  log loss {o['survival_log_loss']}  "
              f"model match {o['model_match_rate']}  top3 {o['top3_rate']}  "
              f"median rank {o['median_rank']}")
        p = out["predictors"]["predictors"]
        print("   " + "  ".join(f"{n} {s['log_loss']}" for n, s in p.items()))

# Your draft pick by pick against what the model would have taken, priced both
# from the snapshot recorded at each pick and from today's board. $league_id
# locates the snapshots; without it every row is priced from today's board.
[script]
retrospective $league_id='' $slot='0':
    import json
    import os
    import sys

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    env = json.load(open(".mcp.json"))["mcpServers"]["fantasy-draft"]["env"]
    os.environ.update(env)
    from ffdraft import replay, server, watch

    league_id = os.environ["league_id"] or env.get("ESPN_LEAGUE_ID", "")
    league, _ = server._settings()
    b, st = server._build_board(), server._state()
    out = replay.draft_retrospective(
        b, st, league, slot=(int(os.environ["slot"]) or None),
        snapshots=(watch.snapshot_dir(league_id) if league_id else None))

    cov, agr = out["as_of_coverage"], out["as_of_agreement"]
    print(f"slot {out['slot']}{' (yours)' if out['mine'] else ''}: "
          f"{out['picks_reviewed']} of {out['picks_in_the_draft']} picks made")
    print(f"priced from a snapshot {cov['rows_priced_from_a_snapshot']}, "
          f"from today's board {cov['rows_priced_from_todays_board']}")
    print(f"  {cov['note']}")
    if agr["of"]:
        print(f"as-of and today agree on {agr['same_recommendation']} of {agr['of']}")
        print(f"  {agr['note']}")
    print(f"delta basis: {out['delta_basis']}")
    print()
    head = (f"{'pick':>5} {'rd':>3}  {'you took':<24} {'proj':>7}  {'model said':<24} "
            f"{'proj':>7} {'rank':>5} {'your edge':>10} {'actual':>8}  {'basis':<15}")
    print(head)
    print("-" * len(head))
    for r in out["picks"]:
        print(f"{r['pick']:>5} {r['round']:>3}  {str(r['took'])[:24]:<24} "
              f"{r['took_projection']!s:>7}  {str(r['model_pick_today'])[:24]:<24} "
              f"{r['model_pick_projection']!s:>7} {r['your_pick_rank_today']!s:>5} "
              f"{r['your_pick_edge']!s:>10} {r['your_pick_edge_actual']!s:>8}  "
              f"{r['basis']:<15}")
    print("\nthe room around each of your picks")
    for r in out["picks"]:
        cells = " | ".join(("*" if q["yours"] else " ") + f"{q['pick']} {q['player'][:20]}"
                           for q in r["room_around"])
        print(f"  {r['pick']:>5}: {cells}")

# Evaluate blend_pos (the blend plus league-level position intercepts) against
# the shipped blend on the recorded draft. One walk-forward pass scores both on
# the same picks in the same order, so every pick is a paired observation.
# Reports blocks and spread, never a bare mean: rounds as blocks (the unit the
# correlation lives in), the draft's two halves as disjoint samples, and a
# round-level block bootstrap run as two disjoint seed blocks. $reps is the
# bootstrap replicates per seed block.
[script]
blendpos $reps='2000':
    import json
    import math
    import os
    import sys

    import numpy as np

    def sign_p(blocks) -> float:
        """Two-sided sign test on a set of block signs.

        `blocks_agree` is a boolean and `adp` now publishes what it is worth
        under the null (2^-(k-1)). This is the same currency for a split that is
        not unanimous: 7 of 8 blocks pointing one way is not agreement, and it is
        also not nothing, so report the probability of a split at least this
        lopsided from a term that does nothing rather than a flag either way.
        """
        signs = [b for b in blocks if b != 0]
        n = len(signs)
        if not n:
            return 1.0
        k = max(sum(1 for b in signs if b > 0), sum(1 for b in signs if b < 0))
        tail = sum(math.comb(n, i) for i in range(k, n + 1))
        return min(1.0, 2 * tail / 2 ** n)

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    env = json.load(open(".mcp.json"))["mcpServers"]["fantasy-draft"]["env"]
    os.environ.update(env)
    from ffdraft import replay, server

    league, _ = server._settings()
    b, st = server._build_board(), server._state()
    print(f"board {len(b)} rows, "
          f"market join version {b['market_join_version'].iloc[0] if 'market_join_version' in b.columns else 'n/a'}, "
          f"{len(st.picks)} recorded picks")
    out = replay.replay_draft(b, st, league, adp_shift=replay.room_drift(b, st)["shift"],
                              team_effects=True)
    pr = out["predictors"]
    print(f"sample: {pr['picks_scored']} picks scored, {pr['picks_unscored']} not priced by the "
          f"board {pr['unscored_picks']}")
    for name, s in pr["predictors"].items():
        print(f"  {name:<11} log loss {s['log_loss']!s:>6}  top1 {s['top1']!s:>6}  "
              f"top3 {s['top3']!s:>6}  top5 {s['top5']!s:>6}  median rank {s['median_rank']}")

    rows = [r for r in out["predictor_rows"] if r["scored"]]
    rounds = np.array([(r["pick"] - 1) // league.teams + 1 for r in rows])
    base = np.array([r["blend"]["log_loss"] for r in rows], dtype=float)
    cand = np.array([r["blend_pos"]["log_loss"] for r in rows], dtype=float)
    delta = cand - base                      # negative favours blend_pos
    print(f"paired on {len(delta)} picks; mean delta {delta.mean():+.3f} "
          f"(negative favours blend_pos)")

    print("blocks: draft rounds (the unit the correlation lives in)")
    per_round = []
    for rnd in sorted(set(rounds.tolist())):
        d = delta[rounds == rnd]
        per_round.append(float(d.mean()))
        print(f"  round {rnd:>2}  n {len(d):>3}  delta {d.mean():+.3f}")
    per_round_arr = np.array(per_round)
    agree = bool(np.all(per_round_arr > 0) or np.all(per_round_arr < 0))
    print(f"  round blocks: mean {per_round_arr.mean():+.3f}  "
          f"spread {per_round_arr.max() - per_round_arr.min():.3f}  "
          f"favouring blend_pos in {(per_round_arr < 0).sum()} of {len(per_round_arr)}  "
          f"blocks_agree {agree}  "
          f"agree_p_null {0.5 ** (len(per_round_arr) - 1):.4f}  "
          f"sign test p {sign_p(per_round_arr.tolist()):.3f}")
    if len(per_round_arr) > 1:
        se = per_round_arr.std(ddof=1) / np.sqrt(len(per_round_arr))
        print(f"  paired t over round blocks: {per_round_arr.mean() / se:+.2f} on "
              f"{len(per_round_arr) - 1} df")

    # The rank metrics get the same treatment: a bare top-3 rate is an estimate
    # too, and the difference between two of them needs its spread beside it.
    print("blocks: the rank metrics, paired per round")
    base_rank = np.array([r["blend"]["rank"] for r in rows], dtype=float)
    cand_rank = np.array([r["blend_pos"]["rank"] for r in rows], dtype=float)
    for k in (1, 3, 5):
        d = (cand_rank <= k).astype(float) - (base_rank <= k).astype(float)
        blocks = np.array([d[rounds == r].mean() for r in sorted(set(rounds.tolist()))])
        agree_k = bool(np.all(blocks > 0) or np.all(blocks < 0))
        print(f"  top{k}  blend {(base_rank <= k).mean():.3f} -> blend_pos "
              f"{(cand_rank <= k).mean():.3f}  delta {d.mean():+.3f}  "
              f"round spread {blocks.max() - blocks.min():.3f}  "
              f"blocks_agree {agree_k}  sign test p {sign_p(blocks.tolist()):.3f}")

    print("blocks: the draft's two halves, as disjoint samples")
    order = np.argsort([r["pick"] for r in rows])
    half = len(order) // 2
    halves = []
    for label, idx in (("first", order[:half]), ("second", order[half:])):
        d = delta[idx]
        halves.append(float(d.mean()))
        print(f"  {label:<7} picks {rows[idx[0]]['pick']:>3}-{rows[idx[-1]]['pick']:<3} "
              f"n {len(d):>3}  blend {base[idx].mean():.3f}  blend_pos {cand[idx].mean():.3f}  "
              f"delta {d.mean():+.3f}")
    print(f"  half blocks: spread {max(halves) - min(halves):.3f}  "
          f"blocks_agree {bool(halves[0] * halves[1] > 0)}")

    print("blocks: round-level bootstrap, two disjoint seed blocks")
    reps = int(os.environ["reps"])
    uniq = np.array(sorted(set(rounds.tolist())))
    points = []
    for block, seed in enumerate((0, reps)):
        rng = np.random.default_rng(seed)
        draws = np.empty(reps)
        for i in range(reps):
            picked = rng.choice(uniq, size=len(uniq), replace=True)
            draws[i] = np.mean([delta[rounds == r].mean() for r in picked])
        lo, hi = np.percentile(draws, [2.5, 97.5])
        points.append(float(draws.mean()))
        print(f"  block {block + 1} seeds {seed}-{seed + reps - 1}: mean {draws.mean():+.3f}  "
              f"95% [{lo:+.3f}, {hi:+.3f}]  P(favours blend_pos) {(draws < 0).mean():.2f}")
    print(f"  seed blocks: spread {abs(points[0] - points[1]):.3f}  "
          f"blocks_agree {bool(points[0] * points[1] > 0)}")

    # One line saying what this carries, in the same shape as adp.block_verdict
    # and refusing the same word.
    p = sign_p(per_round_arr.tolist())
    effect, spread = abs(per_round_arr.mean()), per_round_arr.max() - per_round_arr.min()
    print("verdict: " + (
        f"the round blocks disagree in sign (sign test p {p:.3f}) and the spread between "
        f"them is {spread / effect:.1f}x the effect, so this improvement is inside the "
        "harness's own noise and supports nothing"
        if not agree or p > 0.05 else
        f"the round blocks agree in sign (p {p:.3f}) — an observation, not a pass, and it "
        "says nothing about the magnitude"))

# Score choice.py's per-team predictor against the plain blend on the recorded
# draft. One walk-forward pass scores both on the same picks in the same order,
# so the log losses are directly comparable. $l2 overrides choice.TEAM_L2 to see
# how the answer moves with the shrinkage.
[script]
teameffects $l2='':
    import json
    import os
    import sys

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    env = json.load(open(".mcp.json"))["mcpServers"]["fantasy-draft"]["env"]
    os.environ.update(env)
    from ffdraft import choice, replay, server

    if os.environ["l2"]:
        choice.TEAM_L2 = float(os.environ["l2"])
    league, _ = server._settings()
    b, st = server._build_board(), server._state()
    out = replay.replay_draft(b, st, league, adp_shift=replay.room_drift(b, st)["shift"],
                              team_effects=True)
    s = out["predictors"]["predictors"]
    print(f"team effects: TEAM_L2 {choice.TEAM_L2} vs league L2 {choice.L2}; "
          f"{out['predictors']['picks_scored']} picks scored out of sample")
    for name, r in s.items():
        print(f"  {name:<11} log loss {r['log_loss']!s:>6}  top1 {r['top1']!s:>6}  top3 {r['top3']!s:>6}  "
              f"top5 {r['top5']!s:>6}  median rank {r['median_rank']}")
    def delta(a, b):
        if not (s.get(a) and s.get(b)) or s[a]["log_loss"] is None or s[b]["log_loss"] is None:
            return
        d = s[a]["log_loss"] - s[b]["log_loss"]
        print(f"{a} - {b} log loss: {d:+.3f} ({'BETTER' if d < 0 else 'WORSE'} out of sample)")

    # blend_pos is the control: the same features without the team deviations.
    # blend_pos - blend is what the position intercepts buy; blend_team -
    # blend_pos is what being per-team buys on top of them.
    delta("blend_pos", "blend")
    delta("blend_team", "blend_pos")
    delta("blend_team", "blend")
    fc = out.get("forecast") or {}
    for slot, dev in sorted((fc.get("team_deviations") or {}).items(), key=lambda kv: int(kv[0])):
        biggest = max(dev.items(), key=lambda kv: abs(kv[1]))
        print(f"  slot {slot:>2} deviation  " + ", ".join(f"{k} {v:+.3f}" for k, v in dev.items())
              + f"   | largest {biggest[0]} {biggest[1]:+.3f}")

# SIMULATION. Replay the draft with the model drafting for $slot (yours by
# default) while every other team drafts per the walk-forward blend predictor.
# $policy is argmax (deterministic) or sample. Same numbers as the
# draft_counterfactual tool, without a server.
[script]
counterfactual $slot='0' $policy='argmax' $seed='0':
    import json
    import os
    import sys

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    env = json.load(open(".mcp.json"))["mcpServers"]["fantasy-draft"]["env"]
    os.environ.update(env)
    from ffdraft import replay, server

    league, _ = server._settings()
    b, st = server._build_board(), server._state()
    slot = int(os.environ["slot"]) or st.my_slot
    out = replay.counterfactual_draft(b, st, league, slot, policy=os.environ["policy"],
                                      seed=int(os.environ["seed"]))
    print(out["note"])
    print(f"slot {out['slot']}{' (yours)' if out['mine'] else ''}  picks replayed {out['picks_replayed']}  "
          f"substitutions {out['substitutions_made']} of {len(out['substitutions'])}")
    d = out["divergence"]
    print(f"other teams: {d['other_team_picks_changed']} of {d['other_team_picks']} picks differ from the real draft; "
          f"{d['mirrored_off_board']} off-board picks mirrored, {d['pool_exhausted']} picks past an empty pool; "
          f"the control could not have {d['control_picks_unavailable']} of its real picks")
    s, bn, o = out["starters_proj"], out["bench_proj"], out["open_starter_slots"]
    print(f"projected starter points: model {s['model']}  control {s['control']}  real {s['real']}")
    print(f"  vs control (same room, real picks mirrored): {s['delta_vs_control']:+}   <- the intervention")
    print(f"  vs real    (also carries the room difference): {s['delta_vs_real']:+}")
    print(f"bench: model {bn['model']} control {bn['control']} real {bn['real']}   "
          f"open starter slots: model {o['model']} control {o['control']} real {o['real']}")
    print("substitutions (real -> model, and what the control took)")
    for r in out["substitutions"]:
        mark = "  =" if r["same"] else "  ->"
        control = "" if r["control_is_real"] else f"   control {r['control']} {r['control_proj']!s}"
        print(f"  pick {r['pick']:>3} r{r['round']:<2} {r['real']:<26} {r['real_position']!s:<3} {r['real_proj']!s:>6}"
              f"{mark} {r['model']:<26} {r['model_position']!s:<3} {r['model_proj']!s:>6}{control}")
    for label, rows in (("model roster", out["model_roster"]), ("control roster", out["control_roster"]),
                        ("real roster", out["real_roster"])):
        print(label)
        for r in rows:
            print(f"  pick {r['pick']:>3} r{r['round']:<2} {r['player']:<26} {r['position']!s:<3} {r['proj_points']!s:>6}")

# The draft_audit tool without a server: invariants between board, picks and
# recommendation, plus the market-join report (rows priced synthetically, rows
# priced through an alias).
[script]
audit:
    import json
    import os
    import sys

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    env = json.load(open(".mcp.json"))["mcpServers"]["fantasy-draft"]["env"]
    os.environ.update(env)
    from ffdraft import server

    b = server._build_board()
    flags = {c: str(b[c].dtype) for c in ("is_rookie", "off_roster") if c in b.columns}
    special = int(b["position"].isin(("K", "DST")).sum()) if "position" in b.columns else 0
    print(f"board rows {len(b)}  K/DST rows {special}  market_join_version "
          f"{int(b['market_join_version'].iloc[0]) if 'market_join_version' in b.columns else None}  "
          f"adp_source {b['adp_source'].value_counts().to_dict() if 'adp_source' in b.columns else None}  "
          f"flag dtypes {flags}")
    out = json.loads(server.draft_audit())
    print(f"ok {out['ok']}  picks {out['picks']}  mine {out['mine']}  unresolved {out['unresolved']}")
    for f in out["failures"]:
        print("FAIL", f)
    for w in out["warnings"]:
        print("warn", w)
    mj = out["market_join"]
    print(f"market says undrafted (ESPN's placeholder ADP, priced synthetically): {mj['undrafted_total']}")
    print(f"market join: {mj['unjoined_total']} rows priced synthetically; strongest projections:")
    for u in mj["unjoined"]:
        print(f"  {u['name']:<26} {u['position']:<3} {u.get('team', '')!s:<4} proj {u['proj_points']:>6} synthetic adp {u['synthetic_adp']:>6}")
    print(f"priced through an alias: {mj['alias_joined_total']}")
    for a in mj["alias_joined"]:
        print(f"  {a['name']:<26} {a['position']:<3} {a['how']:<17} adp {a['adp']:>6}")
    print(f"priced on the name alone (market lists another position): {mj['key_only_total']}")
    for a in mj["key_only"]:
        print(f"  {a['name']:<26} {a['position']:<3} adp {a['adp']:>6}")

# What the team on the clock (or $slot) should take, what ESPN's list says, how
# that team has been choosing, and the prediction. Same as the predict_pick tool.
[script]
predict $slot='0':
    import json
    import os
    import sys

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    env = json.load(open(".mcp.json"))["mcpServers"]["fantasy-draft"]["env"]
    os.environ.update(env)
    from ffdraft import replay, server

    league, _ = server._settings()
    b, st = server._build_board(), server._state()
    slot = int(os.environ["slot"]) or st.slot_for_pick(st.on_the_clock)
    out = replay.predict_pick(b, st, league, slot, adp_shift=replay.room_drift(b, st)["shift"])
    if slot == st.slot_for_pick(st.on_the_clock):
        rp = replay.replay_draft(b, st, league, adp_shift=replay.room_drift(b, st)["shift"])
        out["forecast"], out["predictors"] = rp.get("forecast"), rp["predictors"]
    print(f"slot {slot}: pick {out['on_the_clock']} on the clock, next {out['next_pick']}, roster {out['roster']}, "
          f"open starters {out['open_starter_slots']}")
    t = out["tendency"]
    print(f"tendency: median ESPN passes {t['median_espn_passes']}, follows ESPN list {t['follows_espn_list']}, "
          f"positions {t['positions']}")
    for h in out["history"]:
        print(f"  pick {h['pick']:>3} {h['player']:<26} {h['position']!s:<3} espn rank {h['espn_rank']!s:>4} passed {h['espn_passes']!s:>3}")
    print("should (model):")
    for s in out["should"]:
        print(f"  {s['player']:<26} {s['position']:<3} proj {s['proj_points']:>6} value {s['pick_value']:>7}")
    print("ESPN list next:")
    for e in out["espn_list"]:
        print(f"  {e['player']:<26} {e['position']:<3} rank {e['espn_rank']:>4} adp {e['adp']:>6}")
    p = out["predicted"]
    print(f"predicted (tendency rule): {p['player']} ({p['position']}) -- {p['basis']}")
    fc = out.get("forecast")
    if fc:
        print(f"walk-forward forecast (picks scored so far: {out['predictors']['picks_scored']}):")
        print("  position: " + ", ".join(f"{k} {v:.0%}" for k, v in fc["position_probabilities"].items()))
        for name in ("blend", "espn_list", "adp", "model"):
            print(f"  {name:<10} " + "; ".join(f"{c['player']} {c['p']:.0%}" for c in fc[name]))
        for name, s in out["predictors"]["predictors"].items():
            print(f"    {name:<10} out-of-sample log loss {s['log_loss']!s:>6} top1 {s['top1']!s:>6} top3 {s['top3']!s:>6}")

# Dump everything ESPN reports about a league's draft into $out_dir (default: cwd).
# Cookies come from .mcp.json. Opens the draft room once for the snapshot, which
# bumps a browser room or a running watch; use the dump_draft tool while watching.
[script]
dump $league_id $out_dir='.':
    import json
    import os
    import sys

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    env = json.load(open(".mcp.json"))["mcpServers"]["fantasy-draft"]["env"]
    os.environ.update(env)
    from ffdraft import board as bd
    from ffdraft import espn_dump
    from ffdraft.config import CURRENT_SEASON

    league_id, out_dir = os.environ["league_id"], os.environ["out_dir"]
    season = int(os.environ.get("FFDRAFT_SEASON", CURRENT_SEASON))
    info = bd.espn_league_context(league_id, season, env["ESPN_SWID"], env["ESPN_S2"])
    m = espn_dump.dump_draft(league_id, out_dir, season, team_id=info["my_team_id"])
    print(m["root"])
    for e in m["read_api"]:
        print(f"  {e['view']:<26} {e['status']} {e['bytes']:>9} bytes")
    for e in m["live"]:
        print(f"  live/{e['file']:<21} {json.dumps({k: v for k, v in e.items() if k != 'file'})}")
    if m["errors"]:
        print("errors:", *m["errors"], sep="\n  ")

# Office presence and chat report from a dump: who was in the draft room, how
# long, who talked, and how long each pick took. No cookies, no network.
[script]
roomstats $dump_dir='.':
    import json
    import os
    import sys

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    from ffdraft import roomstats

    root = roomstats.find_dump(os.environ["dump_dir"])
    if root is None:
        sys.exit(f"no espn_dump_* directory under {os.environ['dump_dir']!r}; run `just dump` first")
    stats = roomstats.room_stats(roomstats.from_dump(root))
    print(roomstats.format_table(stats))
    out = root / "room_stats.json"
    out.write_text(json.dumps(stats, indent=1), encoding="utf-8")
    print(f"\njson -> {out}")

# Evidence for the roles.py features: opportunity-share coverage on the live
# board, whether role entropy marks the projections that miss, and paired mock
# drafts with and without each pick_value weight. Numbers go in CHANGELOG.md.
[script]
roles $what='all' $seasons='2024,2025' $trials='8' $seed='0':
    import json
    import os
    import sys

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    env = json.load(open(".mcp.json"))["mcpServers"]["fantasy-draft"]["env"]
    os.environ.update(env)
    import numpy as np

    from ffdraft import board as bd
    from ffdraft import model, roles, server

    what = os.environ["what"]
    seasons = [int(s) for s in os.environ["seasons"].split(",") if s.strip()]
    trials = int(os.environ["trials"])
    league, weights = server._settings()
    print(f"league: {league.teams} teams, starters {league.starters}, "
          f"slots counted for start probability {roles.position_slots(league)}")

    if what in ("all", "shares"):
        b = server._build_board()
        print(f"\n== opportunity shares on the live board ({len(b)} rows)")
        for col in roles.OPPORTUNITY_COLUMNS:
            s = b[col]
            print(f"  {col:<15} non-null {int(s.notna().sum()):>4}  median {s.median():.3f}  "
                  f"p90 {s.quantile(0.9):.3f}  max {s.max():.3f}")
        # The shares are read-only: prove pick_value does not move.
        b2 = b.copy()
        b2["drafted"] = False
        a = model.recommend(b2, league, current_pick=1, next_pick=32, top_n=200)
        c = model.recommend(b2.drop(columns=list(roles.OPPORTUNITY_COLUMNS)), league,
                            current_pick=1, next_pick=32, top_n=200)
        same = bool((a["name"].tolist() == c["name"].tolist())
                    and np.allclose(a["pick_value"], c["pick_value"]))
        print(f"  pick_value identical with and without the share columns: {same}")

    if what in ("all", "entropy"):
        print("\n== role entropy vs projection error, leak-free board per season")
        print("   (past seasons have no ESPN projection, so this scores the churn half)")
        for season in seasons:
            tbl = model.build_player_table(league, weights, season=season)
            proj = model.project(tbl, league, weights)
            proj = roles.attach_role_entropy(
                proj, roles.snap_share_churn(list(range(season - 5, season))))
            out = roles.entropy_error_backtest(proj, season, league)
            print(f"  {season}: n {out['n']}, spread {out['spread']}")
            for row in out["bins"]:
                print(f"    bin {row['bin']} n {row['n']:>3} entropy {row['entropy_mean']:.3f} "
                      f"abs pct error {row['abs_pct_error']:.3f}")

    if what in ("all", "weights", "start_prob", "handcuff"):
        configs = (("start_prob", {"start_prob": 1.0}),
                   ("handcuff", {"handcuff": 1.0}),
                   ("both", {"start_prob": 1.0, "handcuff": 1.0}))
        if what in ("start_prob", "handcuff"):
            configs = tuple(c for c in configs if c[0] in (what, "both"))
        for label, rw in configs:
            print(f"\n== paired mock drafts, {label} on vs off ({trials} trials/season)")
            out = roles.weight_backtest(league, weights, seasons, rw, n_trials=trials,
                                        seed=int(os.environ["seed"]),
                                        progress=lambda m: print("   " + m, flush=True))
            for s in out["seasons"]:
                if "error" in s:
                    print(f"  {s['season']}: {s['error']}")
                    continue
                print(f"  {s['season']}: improvement {s['improvement']:+} across blocks "
                      f"{s['block_improvements']}, spread {s['block_spread']}, "
                      f"blocks agree {s['blocks_agree']}, "
                      f"{s['trials_improved_of_changed']}/{s['trials_changed']} of the trials "
                      f"it changed")
                for blk in s["blocks"]:
                    print(f"    block {blk['block']} (seeds {blk['seed_from']}..): off "
                          f"{blk['weekly_points_off']} on {blk['weekly_points_on']} "
                          f"improvement {blk['improvement']:+}, "
                          f"{blk['trials_improved_of_changed']}/{blk['trials_changed']} of the "
                          f"trials it changed, empty slots {blk['empty_slots_off']} -> "
                          f"{blk['empty_slots_on']}")
            print(f"  overall improvement {out['overall_improvement']}, blocks agree "
                  f"{out['blocks_agree']}, worst block spread {out['worst_block_spread']}, "
                  f"{out['players_swapped']} players swapped")
            print("  " + adp_mod.block_verdict(out))

# Does the bye-week stacking penalty win weekly lineup points? Two disjoint seed
# blocks per season, both reported: a mean whose blocks disagree is noise.
[script]
bye $seasons='2022,2023,2024,2025' $trials='12' $weight='0.08' $seed='0':
    import json
    import os
    import sys

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    env = json.load(open(".mcp.json"))["mcpServers"]["fantasy-draft"]["env"]
    os.environ.update(env)
    from ffdraft import adp, server

    league, weights = server._settings()
    seasons = [int(s) for s in os.environ["seasons"].split(",") if s.strip()]
    out = adp.bye_backtest(league, weights, seasons, n_trials=int(os.environ["trials"]),
                           bye_weight=float(os.environ["weight"]),
                           seed=int(os.environ["seed"]),
                           progress=lambda m: print("   " + m, flush=True))
    print(f"\n== bye_weight {out['bye_weight']}, {out['n_trials']} trials x "
          f"{out['n_blocks']} blocks per season")
    for s in out["seasons"]:
        if "error" in s:
            print(f"  {s['season']}: {s['error']}")
            continue
        print(f"  {s['season']}: improvement {s['improvement']:+} across blocks "
              f"{s['block_improvements']}, spread {s['block_spread']}, "
              f"blocks agree {s['blocks_agree']}, "
              f"{s['trials_improved_of_changed']}/{s['trials_changed']} of the trials it changed")
        for blk in s["blocks"]:
            print(f"    block {blk['block']} (seeds {blk['seed_from']}..): off "
                  f"{blk['weekly_points_off']} on {blk['weekly_points_on']} improvement "
                  f"{blk['improvement']:+}, empty slots {blk['empty_slots_off']} -> "
                  f"{blk['empty_slots_on']}")
    print(f"  overall improvement {out['overall_improvement']}, blocks agree "
          f"{out['blocks_agree']}, worst block spread {out['worst_block_spread']}")
    print("  " + adp.block_verdict(out))

# What the room's own record says about K and D/ST survival, against what ESPN
# ADP says, at each of your remaining picks. The ADP column is the shipped
# number; `room` is (1 - r)^h from the position's observed take-rate. Measured
# at the pick on the clock; later picks are a projection at today's rate, and
# the header says so.
[script]
takerate:
    import json
    import os
    import sys

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    env = json.load(open(".mcp.json"))["mcpServers"]["fantasy-draft"]["env"]
    os.environ.update(env)
    from ffdraft import model, server
    from ffdraft.board import norm_name

    league, weights = server._settings()
    st = server._state()
    # Mark the drafted rows: on the raw board every pick is still "available",
    # which puts round-one players in the pool at pick 132 and makes every
    # number below meaningless.
    b = server._mark_drafted(server._build_board(), st)
    pos_of = b.set_index("_key")["position"].to_dict()
    n = len(st.picks)
    taken = {}
    for p in st.picks:
        pos = pos_of.get(norm_name(p["name"])) or p.get("position")
        if pos:
            taken[pos] = taken.get(pos, 0) + 1
    total = league.teams * league.rounds
    print(f"board {len(b)} rows; {n} picks recorded; {league.teams} teams x {league.rounds} "
          f"rounds = {total} picks; on the clock {st.on_the_clock}")
    print("room record by position: "
          + ", ".join(f"{k} {v}" for k, v in sorted(taken.items(), key=lambda kv: -kv[1])))

    avail = b[~b["drafted"]] if "drafted" in b.columns else b
    mine = [p for p in st.my_picks() if p >= st.on_the_clock]
    print(f"your remaining picks: {mine}")
    for pos in ("K", "DST"):
        need = league.starters.get(pos, 0) * league.teams
        got = taken.get(pos, 0)
        r_obs = got / n if n else 0.0
        print(f"\n== {pos}: room has taken {got} of {need} in {n} picks "
              f"(observed rate {r_obs:.4f}/pick)")
        rows = avail[avail["position"] == pos].sort_values("draft_score", ascending=False)
        if rows.empty:
            print("   none available")
            continue
        top = rows.iloc[0]
        print(f"   best available: {top['name']} (adp {float(top['adp']):.0f}, "
              f"draft_score {float(top['draft_score']):.1f})")
        scores = rows["draft_score"].tolist()
        held = st.held_by_slot(b)
        print(f"   {'pick':>4} {'next':>4} {'h':>3} {'left':>5} {'slots':>5} "
              f"{'forced':>6} {'takers':>7} {'ADP p0':>7} {'cnt p0':>7} "
              f"{'ADP fb':>7} {'cnt fb':>7} {'ADP mg':>7} {'cnt mg':>7}")
        for cur, nxt in zip(mine, mine[1:]):
            h = nxt - cur
            picks_left = max(0, total - cur)
            slots_left = max(0, need - got)
            hz = model.pick_hazards(league, held, cur, nxt, pos, r_obs)
            forced = sum(1 for x in hz if x > r_obs)
            takers = sum(hz)
            counting = model.counting_survival(hz, len(scores), slots_left)
            adp_p = model.survival_probability_vec(
                rows["adp"].to_numpy(), cur, nxt)

            def fallback(probs, scores=scores):
                """The same walk expected_best_at_next_pick does, one position."""
                expected, gone = 0.0, 1.0
                for score, p in zip(scores, probs):
                    expected += score * p * gone
                    gone *= 1 - p
                    if gone < 0.005:
                        break
                return expected

            fb_adp, fb_cnt = fallback(adp_p), fallback(counting)
            print(f"   {cur:>4} {nxt:>4} {h:>3} {picks_left:>5} {slots_left:>5} "
                  f"{forced:>6} {takers:>7.2f} {adp_p[0]:>7.2f} {counting[0]:>7.2f} "
                  f"{fb_adp:>7.2f} {fb_cnt:>7.2f} {scores[0] - fb_adp:>7.2f} "
                  f"{scores[0] - fb_cnt:>7.2f}")

    cur = st.next_pick_for_me() or st.on_the_clock
    nxt = st.pick_after_next()
    for label, extra in (("ADP survival (before)", {}),
                         ("room survival (after)",
                          {"room_picks": taken, "picks_so_far": n,
                           "room_held": st.held_by_slot(b)})):
        recs = model.recommend(b, league, current_pick=cur, next_pick=nxt,
                               roster=st.my_roster(b), top_n=5, mine=st.my_rows(b),
                               bye_weight=weights.bye, **extra)
        print(f"\nheadline at pick {cur} (next {nxt}), {label}:")
        for _, r in recs.iterrows():
            print(f"   {r['name']:<26} {r['position']:<3} value {float(r['pick_value']):>7.2f}  "
                  f"survives {float(r['p_available_next']):.2f}  "
                  f"fallback {float(r['fallback_value']):>7.2f}  "
                  f"marginal {float(r['marginal_value']):>7.2f}")
        print(f"   why: {model.explain(recs.iloc[0])}")
        full = model.recommend(b, league, current_pick=cur, next_pick=nxt,
                               roster=st.my_roster(b), top_n=len(b), mine=st.my_rows(b),
                               bye_weight=weights.bye, **extra)
        dst = full[full["position"] == "DST"]
        if not dst.empty:
            top_dst = dst.iloc[0]
            rank = int(full.index.get_indexer([top_dst.name])[0]) + 1
            print(f"   best D/ST {top_dst['name']} ranks {rank} of {len(full)}, "
                  f"value {float(top_dst['pick_value']):.2f}")
            print(f"   why: {model.explain(top_dst)}")

    # The last two picks, at today's take-rate: the floor has to make a required
    # position urgent again even though the room has deferred all draft. This is
    # a projection, not a measurement -- the draft is at pick 125.
    print("\nprojection at today's rate, your last two picks:")
    for cur2, nxt2 in ((196, 221), (221, None)):
        recs = model.recommend(b, league, current_pick=cur2, next_pick=nxt2,
                               roster=st.my_roster(b), top_n=3, mine=st.my_rows(b),
                               bye_weight=weights.bye, room_picks=taken, picks_so_far=n,
                               room_held=st.held_by_slot(b))
        head = recs.iloc[0]
        print(f"   pick {cur2} (next {nxt2}): {head['name']} ({head['position']}) "
              f"value {float(head['pick_value']):.2f} survives "
              f"{float(head['p_available_next']):.2f}")

# Probe every external data surface; see docs/data-sources.md
[script]
surfaces:
    import io
    import json
    import os

    import pandas as pd
    import requests

    UA = {"User-Agent": "ffdraft-mcp/1.0"}
    season = int(os.environ.get("FFDRAFT_SEASON", "2026"))
    nflverse = "https://github.com/nflverse/nflverse-data/releases/download"

    def head(url):
        try:
            return requests.head(url, allow_redirects=True, timeout=20, headers=UA).status_code
        except requests.RequestException as exc:
            return type(exc).__name__

    print("== nflverse per-season assets")
    for tag, tmpl in (
        ("stats_player", "stats_player_week_{s}.parquet"),
        ("snap_counts", "snap_counts_{s}.parquet"),
        ("injuries", "injuries_{s}.parquet"),
        ("weekly_rosters", "roster_weekly_{s}.parquet"),
        ("depth_charts", "depth_charts_{s}.parquet"),
        ("pbp", "play_by_play_{s}.parquet"),
    ):
        for s in (season - 1, season):
            print(f"  {tag:<15} {tmpl.format(s=s):<34} {head(f'{nflverse}/{tag}/{tmpl.format(s=s)}')}")
    print("== nflverse single assets")
    for tag, name in (("players", "players.parquet"), ("draft_picks", "draft_picks.parquet"),
                      ("combine", "combine.parquet"), ("nextgen_stats", "ngs_receiving.parquet"),
                      ("nextgen_stats", "ngs_rushing.parquet")):
        print(f"  {tag:<15} {name:<34} {head(f'{nflverse}/{tag}/{name}')}")

    print("== nfldata games.csv")
    g = pd.read_csv("https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv")
    print(f"  seasons {int(g.season.min())}-{int(g.season.max())}, {season} games {int((g.season == season).sum())}, div_game {'div_game' in g.columns}")

    print("== dynastyprocess ECR")
    e = pd.read_parquet("https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_fpecr.parquet",
                        columns=["page_type", "scrape_date"])
    e["scrape_date"] = pd.to_datetime(e["scrape_date"])
    for pt in ("redraft-overall", "redraft-op"):
        sub = e[e["page_type"] == pt]
        print(f"  {pt:<16} latest scrape {sub['scrape_date'].max().date()}  rows {int((sub['scrape_date'] == sub['scrape_date'].max()).sum())}")

    print("== sleeper")
    r = requests.get("https://api.sleeper.app/v1/state/nfl", timeout=20, headers=UA)
    print(f"  {r.status_code} season {r.json().get('season')} week {r.json().get('week')}")

    print("== fantasypros ADP page (server-rendered player table?)")
    r = requests.get("https://www.fantasypros.com/nfl/adp/ppr-overall.php", timeout=30,
                     headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
    tables = pd.read_html(io.StringIO(r.text))
    has_player = any(any("player" in str(c).lower() for c in t.columns) for t in tables)
    print(f"  {r.status_code} tables {len(tables)} player table {has_player}")

    print("== ESPN league read API")
    swid, s2 = os.environ.get("ESPN_SWID"), os.environ.get("ESPN_S2")
    league, team = os.environ.get("ESPN_LEAGUE_ID"), os.environ.get("ESPN_TEAM_ID")
    if not (swid and s2 and league and team):
        print("  skipped: set ESPN_SWID, ESPN_S2, ESPN_LEAGUE_ID, ESPN_TEAM_ID (a team you own)")
    else:
        url = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league}"
        r = requests.get(url, params={"view": ["mDraftDetail", "mTeam", "mSettings"]},
                         cookies={"SWID": swid, "espn_s2": s2}, timeout=20, headers=UA)
        print(f"  {r.status_code}")
        if r.ok:
            d = r.json()
            dd = d.get("draftDetail") or {}
            picks = dd.get("picks") or []
            filled = sum(1 for p in picks if p.get("playerId") not in (None, -1))
            print(f"  drafted {dd.get('drafted')} inProgress {dd.get('inProgress')} picks {len(picks)} filled {filled} teams {len(d.get('teams') or [])}")
            tok = requests.get(f"{url}/teams/{team}/draftSecurity", cookies={"SWID": swid, "espn_s2": s2},
                               headers={**UA, "Accept": "application/json", "X-Fantasy-Source": "kona"}, timeout=20)
            print(f"  draftSecurity {tok.status_code} {json.dumps(tok.text[:40])}")

# Spawn the server as a real stdio subprocess, handshake, list tools, call two
[script]
smoke:
    import asyncio
    import os

    from mcp import Client, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = os.getcwd()
    server = StdioServerParameters(
        command=os.path.join(root, ".venv", "Scripts", "python.exe"),
        args=["-m", "ffdraft.server"],
        env={"FFDRAFT_SEASON": "2026", "PYTHONPATH": os.path.join(root, "src")},
    )

    async def main() -> None:
        # mode="legacy" is the initialize handshake Claude Code uses; "auto" is the
        # 2026 discover probe. The channel capability must show on both.
        for mode in ("legacy", "auto"):
            async with Client(stdio_client(server), mode=mode) as client:
                info = client.server_info
                print(f"{mode}: server {info.name if info else None} protocol "
                      f"{client.protocol_version} experimental "
                      f"{getattr(client.server_capabilities, 'experimental', None)}")
        async with Client(stdio_client(server), mode="legacy") as client:
            tools = (await client.list_tools()).tools
            print(f"tools: {len(tools)}")
            for t in tools:
                print("  ", t.name)
            for name, args in (("list_leagues", {}), ("best_available", {"limit": 5})):
                r = await client.call_tool(name, args)
                print(f"--- {name} is_error={r.is_error}")
                print(r.content[0].text)

    asyncio.run(main())
