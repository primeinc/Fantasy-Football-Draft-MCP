# Governor-owned recipes: the improvement queue and its lease. This file is a
# protected path (improve.PROTECTED); no agent edits it.

set script-interpreter := ['sh', '-euc', 'v="$(just --evaluate venv)"; if [ -z "$v" ] || [ ! -x "$v/Scripts/python.exe" ]; then echo "just: no virtualenv python for this checkout. Run: just setup" >&2; exit 1; fi; exec "$v/Scripts/python.exe" "$0" "$@"']

# One governor tick (ffdraft.runner); $flags: --dry-run (default), --supervised, or empty for unattended
tick $flags='--dry-run':
    "{{ justfile_directory() }}/.venv/Scripts/python.exe" -B -m ffdraft.runner tick $flags

# Record that the user accepts unattended fixer and verifier runs executing fixer-written code unsandboxed; $quote is the user's words
[script]
accept-unsandboxed $quote $confirm='':
    import json
    import os
    import sys

    import pandas as pd

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    from ffdraft import runner

    if os.environ["confirm"] != "accept" or not os.environ["quote"].strip():
        sys.exit("not recorded: pass the user's own words as $quote and confirm=accept")
    runner.POLICY.parent.mkdir(parents=True, exist_ok=True)
    runner.POLICY.write_text(json.dumps({"unsandboxed_tests_accepted": True,
                                         "user_quote": os.environ["quote"],
                                         "recorded_utc": pd.Timestamp.now(tz="UTC").isoformat()}),
                             encoding="utf-8")
    print(f"recorded {runner.POLICY}")

# The Task Scheduler definition for the tick every $minutes (parallel instances, 3 h limit); registers it only when $confirm is 'install'
[script]
install $minutes='30' $confirm='':
    import os
    import subprocess
    import sys

    import pandas as pd

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    from ffdraft import runner

    repo = os.getcwd()
    xml = runner.task_xml(int(os.environ["minutes"]), os.path.join(repo, ".venv", "Scripts", "python.exe"),
                          repo, pd.Timestamp.now().floor("min").strftime("%Y-%m-%dT%H:%M:%S"))
    path = runner.STATE_DIR / "runner-task.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-16")
    cmd = ["schtasks", "/Create", "/TN", runner.TASK_NAME, "/XML", str(path), "/F"]
    print(xml)
    print(" ".join(cmd))
    if os.environ["confirm"] != "install":
        sys.exit("not installed: pass confirm=install once the user has said yes")
    subprocess.run(cmd, check=True)

# Remove the scheduled tick; runs schtasks only when $confirm is 'uninstall'
[script]
uninstall $confirm='':
    import os
    import subprocess
    import sys

    cmd = ["schtasks", "/Delete", "/TN", "ffdraft-governor", "/F"]
    print(" ".join(cmd))
    if os.environ["confirm"] != "uninstall":
        sys.exit("not removed: pass confirm=uninstall")
    subprocess.run(cmd, check=True)

# The queue in priority order with each item's effective risk class
[script]
queue:
    import os
    import sys

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    from ffdraft import improve

    items, errors = improve.load()
    for e in errors:
        print(f"invalid: {e}")
    for i in items:
        print(f"{i['status']:<8} {improve.effective_risk(i)} {i['estimated_minutes']:>3}m  "
              f"{i['id']:<28} {i['title']}")

# The item this tick may take under controller-state.json, or why none
[script]
pick:
    import json
    import os
    import sys

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    from ffdraft import governor, improve

    state = json.loads(governor.CONTROLLER_STATE.read_text(encoding="utf-8"))
    eng = state["engineering"]
    print(f"mode {state['mode']} as of {state['as_of']}; engineering {eng}")
    items, errors = improve.load()
    if errors:
        sys.exit("queue invalid: " + "; ".join(errors))
    chosen = improve.pick(items, int(eng["max_minutes"]), eng["max_risk_class"]) if eng["allowed"] else None
    print(json.dumps(chosen, indent=1) if chosen else "nothing to take")

# Lease $item under controller-state.json
[script]
lease $item:
    import json
    import os
    import sys

    import pandas as pd

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    from ffdraft import governor, improve

    state = json.loads(governor.CONTROLLER_STATE.read_text(encoding="utf-8"))
    items, errors = improve.load()
    if errors:
        sys.exit("queue invalid: " + "; ".join(errors))
    match = [i for i in items if i["id"] == os.environ["item"]]
    if not match:
        sys.exit(f"no queue item {os.environ['item']}")
    print(json.dumps(improve.acquire(match[0], state, pd.Timestamp.now(tz="UTC").isoformat())))

# Release the held lease, recording $outcome
[script]
release $outcome:
    import json
    import os
    import sys

    import pandas as pd

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    from ffdraft import improve

    print(json.dumps(improve.release(os.environ["outcome"], pd.Timestamp.now(tz="UTC").isoformat())))

