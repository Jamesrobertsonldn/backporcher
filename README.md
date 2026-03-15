# Voltron

A fully autonomous software engineering pipeline. Label a GitHub issue with `voltron`, and in ~20 minutes you get a merged PR with tests passing and the issue closed — no human in the loop.

Built in 24 hours. 100% auto-merge rate on its first production run (15 PRs, zero manual interventions). This mirrors the agent orchestration architectures emerging from Anthropic's Claude Code and Augment's multi-agent systems — but as a standalone, open-source daemon you can run on your own infra.

## What makes it different

Most "AI coding" tools are glorified autocomplete. Voltron is an **end-to-end pipeline**: it triages, plans dependencies, dispatches sandboxed agents, reviews their work with a coordinator agent, retries CI failures with error context, and merges — all autonomously.

The key insight: treat agents like junior developers. Give them isolated worktrees, review their PRs, and let CI be the final gate. No magic — just good engineering around `claude -p`.

## The Pipeline

```
GitHub Issue (label: voltron)
  → Haiku triages complexity (sonnet vs opus)
    → Batch orchestrator assigns priorities + dependency chains
      → Sandboxed claude -p in git worktree
        → Build verification (optional, per-repo)
          → PR created
            → Coordinator reviews diff for bugs, conflicts, scope
              → CI monitor (auto-retries up to 3x with error context)
                → Orchestrator mode: hold for approval -or- auto-merge
                  → Issue closed
```

For 2+ issues in the same repo, a single Haiku call batch-orchestrates all of them — assigning models, priorities, and identifying which issues must be serialized (e.g., both touching the same component).

## Orchestrator Mode

Voltron defaults to **review-merge** mode: everything is automatic except the final merge to main, which requires `voltron approve <id>` or a click on the web dashboard. This gives you full visibility and a kill switch without slowing down the pipeline.

Three modes via `VOLTRON_APPROVAL_MODE`:
- **`full-auto`** — hands-off, merge on CI pass (the original behavior)
- **`review-merge`** — pause before merge, approve via CLI or dashboard (default)
- **`review-all`** — pause before dispatch AND before merge

Pre-dispatch conflict detection (powered by Haiku, ~$0.001/call) automatically serializes tasks that would touch overlapping files. Global pause/resume lets you freeze the queue without stopping in-flight work.

## Quick Start

```bash
# Install
pip install -e .

# Register a repo
voltron repo add https://github.com/owner/repo

# Optional: set build verification
voltron repo verify myrepo "npm test"

# Set up sandbox user (one-time, requires root)
sudo bash scripts/setup-sandbox.sh

# Configure systemd (edit the env vars first)
sudo cp voltron.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now voltron

# Create an issue to test
gh issue create --repo owner/repo \
  --title "Add a health check endpoint" \
  --body "Add GET /health returning 200 OK" \
  --label voltron

# Watch it work
voltron fleet
journalctl -u voltron -f
```

## CLI

```bash
voltron fleet              # Live dashboard — what's running, queued, reviewing
voltron status <id>        # Task detail with logs
voltron approve <id>       # Approve a held task (merge or dispatch)
voltron hold <id>          # Manually hold any task
voltron release <id>       # Release a user hold
voltron pause              # Freeze the dispatch queue
voltron resume             # Unfreeze
voltron cancel <id>        # Kill agent, cancel task, restore labels
voltron cleanup            # Remove worktrees for finished tasks
voltron repo add <url>     # Register a GitHub repo
voltron repo verify <n> <cmd>  # Set build verification command
voltron worker             # Run daemon foreground
```

## Web Dashboard

Real-time dark-themed dashboard with SSE updates every 5 seconds. Enable by setting `VOLTRON_DASHBOARD_PASSWORD`. Shows repo breakdown, active agents with elapsed time, pipeline status, and approve/pause buttons.

## Architecture

Six concurrent async loops in a single process:

| Loop | Interval | Job |
|------|----------|-----|
| Issue Poller | 30s | Scans GitHub for `voltron`-labeled issues, batch-orchestrates |
| Task Executor | 5s | Claims queued tasks, runs conflict check, dispatches agents |
| Coordinator | 15s | Reviews PR diffs for bugs, conflicts, scope |
| CI Monitor | 60s | Watches CI, auto-retries with error context, merges or holds |
| Cleanup | 5min | Removes worktrees and remote branches for terminal tasks |
| Dashboard | always | aiohttp web server with SSE, approve buttons, pause/resume |

The codebase is intentionally minimal — no web framework, no ORM, no task queue library. Just asyncio + SQLite + subprocess + `gh` CLI.

## Configuration

All via environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `VOLTRON_MAX_CONCURRENCY` | `2` | Parallel agents |
| `VOLTRON_APPROVAL_MODE` | `review-merge` | `full-auto` / `review-merge` / `review-all` |
| `VOLTRON_AGENT_USER` | (none) | Sandbox user (e.g. `voltron-agent`) |
| `VOLTRON_ALLOWED_USERS` | `montenegronyc` | Comma-separated issue author allowlist |
| `VOLTRON_DEFAULT_MODEL` | `sonnet` | Default agent model |
| `VOLTRON_COORDINATOR_MODEL` | `sonnet` | PR review model |
| `VOLTRON_MAX_CI_RETRIES` | `3` | CI failure retries per task |
| `VOLTRON_MAX_TASK_RETRIES` | `3` | Agent failure retries (escalates sonnet→opus) |
| `VOLTRON_DASHBOARD_PORT` | `8080` | Dashboard port |
| `VOLTRON_DASHBOARD_PASSWORD` | (none) | Dashboard password (required to enable) |

## Security Model

- **Agent sandbox**: `claude -p` runs as `voltron-agent` via `sudo -u` with `prlimit` (500 processes, 2GB file limit)
- **Privilege separation**: `gh` CLI (GitHub API) only runs in the worker process — agents can't post comments, merge PRs, or modify issues
- **Author allowlist**: only issues from specified GitHub users are picked up
- **Credential isolation**: agent user gets copied credentials, can't read admin's `~/.ssh`, `~/.claude`, or env secrets
- **Env scrubbing**: `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, etc. stripped from agent subprocesses
- **systemd hardening**: `PrivateTmp`, `PrivateDevices`, `ProtectSystem=full`, `RestrictNamespaces`, etc.

## Self-Healing

- Stale tasks recovered on restart (working → queued, reviewing → re-review)
- Credentials auto-synced when admin's are newer than agent's
- Transient failures (auth, permissions, stale branches) auto-retry
- Merge conflicts detected and re-queued from fresh main
- Task failure cascades recursively through dependency chains
- Worktrees and remote branches cleaned up automatically

## Smart Retry

When an agent fails, Voltron doesn't just retry blindly:
- **Agent failure**: re-queues with model escalation (sonnet → opus after first failure)
- **Build verification failure**: re-runs agent with error output as context
- **CI failure**: fetches CI logs, re-runs agent with failure context
- **Coordinator rejection**: closes PR, re-queues with reviewer feedback injected into prompt

## GitHub Labels

| Label | Meaning | Set by |
|-------|---------|--------|
| `voltron` | Ready for pickup | User |
| `voltron-in-progress` | Agent working | Daemon |
| `voltron-done` | Merged and closed | Daemon |
| `voltron-failed` | Exhausted retries | Daemon |
| `opus` | Force opus model | User |

## Requirements

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`)
- [GitHub CLI](https://cli.github.com/) (`gh`)
- SQLite (bundled with Python)
- A Claude Max subscription or API key

## License

MIT
