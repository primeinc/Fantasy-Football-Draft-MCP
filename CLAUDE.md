# Commitments — every session in this repo

The user's rules, verbatim, and the mechanism that enforces them. The rules
alone failed on 2026-09-15/16 (an approved Wentz claim went unsent for 25
hours; a Jeudy-for-Wentz offer was nearly sent on an inferred approval), so
they are data now: `~/.ffdraft/state/commitments.json`, `src/ffdraft/commitments.py`.

## The invariant

OPEN USER COMMITMENTS SURVIVE TURNS, TOPIC CHANGES, CODE WORK, WATCHDOG
WAKEUPS, AND TOOLING GAPS UNTIL THEY ARE CONFIRMED COMPLETE, EXPLICITLY
CANCELLED/SUPERSEDED, OR BECOME IMPOSSIBLE.

- A later instruction is cumulative. It adds to open commitments; it does not
  replace them unless it says so.
- A time-bounded commitment outranks untimed discretionary work, engineering
  included.
- A missing capability the agent owns becomes part of the commitment. "No send
  path exists" is a blocker only when the agent cannot create or access one.
- Do not ask for authorization again for the same materially unchanged action.
  Re-reading state before acting is verification, not re-authorization.
- Before ending any turn, enumerate every unresolved user commitment.

## Authorization scope

Authorization attaches to the exact material action authorized, not to the
assistant's inferred objective. "Get Wentz" with no price named authorizes no
trade; a fallback chain named with the approval ("Wentz, else Lock, else
Brissett") is one approval, and the next name in it sends without a new "go".

## Execution surfaces

A blocked implementation path is not a blocked objective. Before reporting
that an authorized action cannot be executed, enumerate and attempt every
currently available execution surface that can lawfully complete the same
action, including native tools, browser/computer-use surfaces, existing
application UI, alternate connected tools, and repo-local capabilities. A
single tool lacking a write method does not establish inability.

For an open time-bounded commitment, inability may be claimed only after all
available execution surfaces have been checked and either failed, are
unavailable, or would materially change the authorized action.

Do not build a new actuator while an existing actuator can complete the
commitment before its deadline. Use the working path first; engineering comes
afterward.

## The mechanism

```text
open_commitment      the approval, written when given: players as ESPN ids,
                     deadline with a zone, fallbacks, the user's words verbatim
submit_claim         send only under an open commitment whose players cover
propose_trade        the transaction (commitment_id); CONFIRMED closes it with
submit_lineup        ESPN's id; anything else leaves it open
block_commitment     the only other exits: blocked with a reason, impossible
cancel_commitment    with why, or cancelled by the user
controller_state     HOT with engineering.allowed false while any is open;
                     OVERDUE past the deadline, never dropped
Stop hook            `just commitments-gate` exits 2 with every open one; a
                     turn ends CONFIRMED or with the record blocked/cancelled
```

The turn-end enumeration is the gate's output. A commitment that is open at
the end of a turn is reported as `BLOCKED-UNTIL-RESOLVED: <action>, DEADLINE
<t>` in the reply, with the reason it is still open.

# Never play sound. Never dial 911 as a test.

Both are banned on this host by `~/.claude/CLAUDE.md`; no one can authorize
the second, including Will.
