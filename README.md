# Voltron

Parallel Claude Code agent dispatcher. Turn GitHub Issues into PRs automatically.

## How it works

1. Create GitHub issues and add the `voltron` label
2. Haiku batch-orchestrates multiple issues: assigns models, priorities, and dependency chains
3. Voltron runs sandboxed Claude agents in git worktrees, respecting dependency order
4. Agents make changes, Voltron creates PRs
5. A coordinator agent reviews each diff for bugs, conflicts, and scope
6. CI runs — failures auto-retry with error context (up to 3x)
7. On success, PRs are auto-merged (squash) and issues are closed

## Quick Start

```bash
# Install
pip install -e .

# Register a repo
voltron repo add https://github.com/owner/repo

# Set up sandbox user (one-time, requires root)
sudo bash scripts/setup-sandbox.sh

# Configure systemd
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

## Architecture

Four concurrent async loops:

| Loop | Interval | Job |
|------|----------|-----|
| Issue Poller | 30s | Scans GitHub for `voltron`-labeled issues, batch-orchestrates priorities + dependencies |
| Task Executor | 5s | Runs Claude agents in sandboxed worktrees, respects dependency order |
| Coordinator | 15s | Reviews PRs for quality, conflicts, scope |
| CI Monitor | 60s | Watches CI, auto-retries, auto-merges |

## Configuration

All via environment variables:

```bash
VOLTRON_MAX_CONCURRENCY=2        # Parallel agents
VOLTRON_AGENT_USER=voltron-agent # Sandbox user
VOLTRON_ALLOWED_USERS=myuser     # Issue author allowlist
VOLTRON_COORDINATOR_MODEL=sonnet # Review model
VOLTRON_MAX_CI_RETRIES=3         # CI retry limit
```

## Security

- Agents run as a restricted system user (`voltron-agent`) with process limits
- GitHub API calls (`gh`) only run in the worker process, never in agent sandboxes
- Only issues from allowlisted authors are processed
- Sensitive env vars stripped from agent subprocesses
- systemd hardening: PrivateTmp, PrivateDevices, ProtectSystem, etc.

## Self-Healing

- Stale tasks recovered on restart (working → queued, reviewing → re-review)
- Credentials auto-synced when admin's are newer than agent's
- Transient failures (auth, permissions) auto-retry instead of permanent failure
- Stale branches cleaned up before worktree creation
- Task failure cascades to all queued dependents in dependency chains

## Requirements

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`)
- [GitHub CLI](https://cli.github.com/) (`gh`)
- SQLite (bundled with Python)
