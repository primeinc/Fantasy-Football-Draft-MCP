# Security

## Reporting a vulnerability

Open a [private security advisory](https://github.com/zacharytran26/Fantasy-Football-Draft-MCP/security/advisories/new)
rather than a public issue. Expect an acknowledgement within a few days.

Please don't file security reports as normal issues — that discloses the problem
publicly before anyone can fix it.

## What this project touches

Worth being clear about, because it's a fantasy football tool that nonetheless handles
credentials and runs code an LLM chose to invoke.

### Your ESPN cookies

Private ESPN leagues require two cookies, `SWID` and `espn_s2`. **These are session
credentials for your ESPN account**, not league-specific tokens. Anyone holding them can
act as you across ESPN's fantasy products.

- Supply them through the `ESPN_SWID` and `ESPN_S2` environment variables only.
- Never commit them. `.gitignore` excludes `.env` and `espn_cookies.json`, but that only
  helps if you use those filenames.
- Never paste them into a chat window, an issue, or a bug report. They will persist in
  the history.
- They're sent only to `lm-api-reads.fantasy.espn.com` and, during a live draft,
  `fantasydraft.espn.com`, over HTTPS/WSS, on requests you trigger. Read
  `board.py:sync_espn` and `espn_live.py` to verify.
- Rotate them by logging out of ESPN, which invalidates the session.

Sleeper needs no credentials at all — its draft API is public. Prefer it where you can.

### Network access

The server makes outbound HTTPS requests to, and only to:

| Host | Purpose |
|---|---|
| `github.com`, `objects.githubusercontent.com` | nflverse data releases |
| `raw.githubusercontent.com` | FantasyPros consensus rank history |
| `api.sleeper.app` | live draft picks (public, unauthenticated) |
| `lm-api-reads.fantasy.espn.com` | ESPN league draft detail, draft-room security token |
| `fantasydraft.espn.com` | ESPN draft room socket, only while a draft is in progress |
| `www.fantasypros.com` | fallback ADP page |

It listens on nothing, opens no ports, and accepts no inbound connections. It talks to
your MCP client over stdio.

### Local files

Written under `~/.ffdraft/` (override with `FFDRAFT_CACHE`, `FFDRAFT_DATA`,
`FFDRAFT_STATE`):

- `cache/` — downloaded public NFL data, ~200 MB
- `data/` — computed player boards
- `state/` — your league settings and draft picks

None of it contains credentials. It's all safe to delete; it rebuilds on the next run.

### Code execution

This is an MCP server, so an LLM decides which tools to call. The tools only read data
and write to the directories above — none of them execute shell commands, evaluate
supplied code, or write outside `~/.ffdraft/`. The `adp_csv_path` argument reads a file
you name; point it only at files you trust.

### Data you get back

Player names and statistics from public sources. No personal data, no analytics, no
telemetry. Nothing is sent anywhere about you or your league.

## Supported versions

Fixes land on the latest release. Given the seasonal nature of the tool, older versions
aren't backported.

| Version | Supported |
|---|---|
| 1.x | Yes |

## Dependencies

Runtime dependencies are listed in `pyproject.toml`. Audit them with:

```bash
pip install pip-audit && pip-audit
```
