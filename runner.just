# Governor-owned recipes: the improvement queue and its lease. This file is a
# protected path (improve.PROTECTED); no agent edits it.

set script-interpreter := ['sh', '-euc', 'v="$(just --evaluate venv)"; if [ -z "$v" ] || [ ! -x "$v/Scripts/python.exe" ]; then echo "just: no virtualenv python for this checkout. Run: just setup" >&2; exit 1; fi; exec "$v/Scripts/python.exe" "$0" "$@"']

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

# The effective risk class of a branch's actual diff against HEAD, declared class $declared
[script]
candidate-risk $branch $declared:
    import os
    import subprocess
    import sys

    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    from ffdraft import improve

    changed = subprocess.run(["git", "diff", "--name-only", f"HEAD...{os.environ['branch']}"],
                             check=True, capture_output=True, text=True).stdout.split()
    print("\n".join(changed))
    print(improve.effective_risk({"risk_class": os.environ["declared"], "scope": changed}))
