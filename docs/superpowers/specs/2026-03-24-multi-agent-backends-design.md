# Multi-Agent Backend Integration for Backporcher

**Date:** 2026-03-24
**Status:** Draft
**Goal:** Add Kimi Code and OpenAI Codex as alternative agent backends alongside Claude Code, with intelligent routing, fallback, and load balancing.

## Motivation

Backporcher currently uses Claude Code exclusively for all agent work. This design adds support for multiple coding agent backends to achieve:

1. **Cost optimisation** — route simpler tasks to cheaper agents (Kimi), reserve Claude for complex work
2. **Capability diversity** — different agents have different strengths per language/task type
3. **Redundancy/fallback** — if Claude is rate-limited or down, fall back to other agents

## CLI Interface Comparison

All three agents support non-interactive subprocess invocation with JSON streaming:

| | `claude -p "prompt"` | `kimi -p "prompt" --print` | `codex exec "prompt"` |
|---|---|---|---|
| Auto-approve | `--dangerously-skip-permissions` | `--yolo` (implied by `--print`) | `--full-auto` or `--yolo` |
| JSON output | `--output-format stream-json` | `--output-format stream-json` | `--json` |
| Working dir | cwd | `-w path` | `-C path` |
| Model select | `--model` | `-m` | `-m` |
| Auth env var | `ANTHROPIC_API_KEY` | `KIMI_API_KEY` | `CODEX_API_KEY` |

## Architecture

### 1. Agent Backend Abstraction

New package `src/backends/` with a common protocol and per-agent implementations.

```
src/backends/
  __init__.py       # AgentBackend protocol, AgentEvent dataclass, registry
  claude.py         # Claude Code backend (extracted from current agent.py)
  kimi.py           # Kimi Code backend
  codex.py          # OpenAI Codex backend
```

**`AgentBackend` protocol:**

```python
class AgentBackend(Protocol):
    name: str

    def build_command(self, prompt: str, model: str, worktree_path: Path, config: Config) -> list[str]:
        """Build the subprocess argv for this agent."""
        ...

    def build_env(self, config: Config) -> dict[str, str] | None:
        """Build sanitised environment for the subprocess. None = inherit (sudo resets)."""
        ...

    def parse_output_line(self, line: str) -> AgentEvent | None:
        """Parse a single line of stdout into a normalised event. Returns None for unrecognised lines (logged as debug, not silently dropped)."""
        ...

    def required_env_vars(self) -> dict[str, str]:
        """Return env vars that MUST be present for this backend (e.g. {'KIMI_API_KEY': '<value>'}). Used by credential sync for sandbox user."""
        ...
```

**`AgentEvent` dataclass** (normalised across all backends):

```python
@dataclass
class AgentEvent:
    type: str          # "text", "result", "error", "tool_use", "progress"
    content: str       # text content or summary
    is_error: bool     # whether this is an error event
    raw: dict          # original parsed JSON for backend-specific inspection
```

### 2. Backend Implementations

**Claude backend** (`claude.py`) — extracted from current hardcoded behavior in `agent.py`:
- Command: `["claude", "-p", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions", "--model", model, prompt]`
- Env: strips `SENSITIVE_ENV_VARS` + `CLAUDECODE` + SSH vars
- Parse: handles `type="assistant"`, `type="result"`, `type="content_block_delta"` events
- Models: `["sonnet", "opus", "haiku"]`

**Kimi backend** (`kimi.py`):
- Command: `["kimi", "-p", prompt, "--print", "--output-format", "stream-json", "-y", "-w", str(worktree_path), "-m", model]`
- Env: ensures `KIMI_API_KEY` is set, strips same sensitive vars
- Parse: Kimi uses Anthropic tool calling — stream-json events are expected to follow the same schema as Claude. The parser shares Claude's logic but logs unrecognised event types at debug level (not silently dropped). If no `type="result"` event is received, the backend falls back to concatenating all text content as the output summary.
- Models: `["kimi-latest"]` (or whatever models Kimi exposes)
- **Pre-implementation gate:** Before writing the Kimi backend, run `kimi -p "echo hello" --print --output-format stream-json -y` on this machine and capture the actual output schema. Adjust parser if it diverges from Claude's schema.

**Codex backend** (`codex.py`):
- Command: `["codex", "exec", prompt, "--json", "--full-auto", "--skip-git-repo-check", "-C", str(worktree_path), "-m", model]`
- Env: ensures `CODEX_API_KEY` is set, strips sensitive vars
- Parse: maps Codex JSONL events (`item.completed`, `turn.completed`, `turn.failed`) to `AgentEvent`
- Models: `["gpt-5", "gpt-5.4", "gpt-5.3-codex"]`

### 3. Backend Registry

A simple dict mapping agent name to backend instance, auto-populated based on availability:

```python
def discover_backends(config: Config) -> dict[str, AgentBackend]:
    """Return available backends. An agent is available if its CLI is in PATH and API key is set."""
    backends = {}
    if shutil.which("claude"):
        backends["claude"] = ClaudeBackend()
    if shutil.which("kimi") and config.kimi_api_key:
        backends["kimi"] = KimiBackend()
    if shutil.which("codex") and config.codex_api_key:
        backends["codex"] = CodexBackend()
    return backends
```

### 4. Triage Enhancement

The triage system currently returns `(model, reason)`. Extended to return `(agent, model, reason)`.

**Updated triage prompt** adds an agents section (dynamically built from `enabled_agents`):
```
## Agents Available
- **claude**: Most capable. Complex multi-file changes, architectural work. Expensive.
- **kimi**: Good general capability, cost-effective. Single/multi-file changes.
- **codex**: OpenAI-backed. Good for straightforward implementations.

Respond: AGENT: <agent> MODEL: <model> — <reason>
```

**Batch orchestration** JSON schema gains an `"agent"` field per issue. Validation: unknown agent → `default_agent`, missing field → `default_agent`. The `valid_agents` set is derived from `enabled_agents` config (not hardcoded).

**Fallback logic:** If triage picks an unavailable agent, fall back through the chain: `claude -> kimi -> codex` (configurable via `BACKPORCHER_FALLBACK_CHAIN`).

**Model validation per-backend:** The existing `Config.allowed_models` tuple is replaced by per-backend model validation. Each backend's `build_command()` uses its own model name; the triage prompt lists models per-agent. If the triage output includes an unrecognised model for the chosen agent, fall back to that backend's default model.

### 5. Config Changes

`src/config.py` gains new fields loaded from environment variables (using `tuple` to match existing frozen dataclass pattern):

```python
# Agent backend configuration
kimi_api_key: str                    # KIMI_API_KEY (default: "")
codex_api_key: str                   # CODEX_API_KEY (default: "")
enabled_agents: tuple[str, ...]      # BACKPORCHER_ENABLED_AGENTS (default: ("claude",))
default_agent: str                   # BACKPORCHER_DEFAULT_AGENT (default: "claude")
fallback_chain: tuple[str, ...]      # BACKPORCHER_FALLBACK_CHAIN (default: ("claude", "kimi", "codex"))
```

The existing `allowed_models` field is deprecated. Model validation moves to per-backend logic.

### 6. Database Schema Migration (v9)

```sql
ALTER TABLE tasks ADD COLUMN agent TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE tasks ADD COLUMN agent_fallback_count INTEGER NOT NULL DEFAULT 0;
```

Three touch points for this migration:
1. **`db_migrations.py`** — add `version < 9` migration block with the ALTER TABLE statements
2. **`db_schema.py`** — bump `SCHEMA_VERSION` to 9, add both columns to the fresh `CREATE TABLE tasks` DDL
3. **`db.py`** — add `"agent"` and `"agent_fallback_count"` to the `allowed` set in `update_task()`, and add `agent` parameter to `create_task()` and `create_task_from_issue()`

Tracks which backend executed each task. Used for:
- Per-agent learnings and success rates
- Dashboard display
- Stats and cost tracking
- Fallback chain position tracking

### 7. Agent Execution Changes

`src/agent.py` `run_agent()` changes:
1. Receives `backend: AgentBackend` parameter (looked up from registry by task's `agent` field)
2. Calls `backend.build_command()` instead of hardcoding Claude CLI args
3. Calls `backend.build_env()` instead of hardcoding env var stripping
4. When `config.agent_user` is set (sudo sandbox mode), `backend.required_env_vars()` is called and those vars are injected via `sudo --preserve-env=KIMI_API_KEY,CODEX_API_KEY` (or written to a credential file that `sync_agent_credentials()` manages). This ensures non-Claude API keys reach the sandboxed subprocess.
5. Stream parsing loop calls `backend.parse_output_line()` instead of inline JSON parsing. Unrecognised lines are logged at debug level (not silently dropped) to help diagnose output format mismatches.
6. Everything else (timeout, logging, pid tracking, log file writing) stays the same

**Fallback on failure:** Agent fallback and model escalation are **separate mechanisms with separate counters:**
- `retry_count` (existing) — counts retries within the same agent. Model escalation (sonnet→opus) fires here, up to `max_task_retries`.
- `agent_fallback_count` (new column, default 0) — counts agent switches. When an agent exhausts its `max_task_retries`, the task is re-queued with the next agent in `fallback_chain` and `retry_count` resets to 0. `agent_fallback_count` increments. Max agent fallbacks = `len(fallback_chain) - 1`.
- `_pick_retry_model()` in `dispatch_helpers.py` only fires for model escalation within the current agent. Agent fallback fires in `dispatch.py` after all retries for the current agent are exhausted.

### 8. Docker Changes

**Dockerfile** additions:
```dockerfile
# Kimi Code CLI (uses uv; container has Python 3.11+ via devtools-base)
RUN uv tool install kimi-cli

# OpenAI Codex CLI (Rust binary, no Node required)
RUN curl -fsSL https://github.com/openai/codex/releases/latest/download/install.sh | sh
```

Note: The `--python 3.13` flag is omitted — the container's existing Python is sufficient. Codex is installed via its Rust binary installer (no npm dependency needed).

**docker-compose.yml** additions:
```yaml
environment:
  - KIMI_API_KEY=${KIMI_API_KEY}
  - CODEX_API_KEY=${CODEX_API_KEY}
  - BACKPORCHER_ENABLED_AGENTS=claude,kimi,codex
```

API keys stored in `.env` file (not committed), passed through as environment variables.

### 9. Dashboard Enhancements

- Task list shows agent name alongside model for each task
- Stats page shows per-agent success rates and average completion time
- Agent health indicators (available/unavailable) in the dashboard header

## File Change Summary

| File | Change |
|---|---|
| `src/backends/__init__.py` | NEW — Protocol, AgentEvent dataclass, registry (`discover_backends`) |
| `src/backends/claude.py` | NEW — Claude backend (extracted from agent.py) |
| `src/backends/kimi.py` | NEW — Kimi backend |
| `src/backends/codex.py` | NEW — Codex backend |
| `src/agent.py` | MODIFY — use backend protocol instead of hardcoded Claude CLI, sudo env injection |
| `src/triage.py` | MODIFY — return `(agent, model, reason)`, validate agent field in batch output |
| `src/prompts.py` | MODIFY — add agent selection to triage/batch/conflict prompts |
| `src/config.py` | MODIFY — add agent config fields, deprecate `allowed_models` |
| `src/db.py` | MODIFY — add `agent` + `agent_fallback_count` params to `create_task()`, `create_task_from_issue()`, `update_task()` allowed set |
| `src/db_schema.py` | MODIFY — bump `SCHEMA_VERSION` to 9, add `agent` + `agent_fallback_count` columns to fresh DDL |
| `src/db_migrations.py` | MODIFY — add v8→v9 migration (ALTER TABLE for `agent` and `agent_fallback_count`) |
| `src/worker.py` | MODIFY — initialise backend registry at startup, pass to dispatch |
| `src/worker_poller.py` | MODIFY — handle `(agent, model, reason)` from triage, store agent on task creation |
| `src/dispatch.py` | MODIFY — look up backend from registry, pass to `run_agent()`, agent fallback logic |
| `src/dispatch_helpers.py` | MODIFY — add `_pick_fallback_agent()` alongside existing `_pick_retry_model()` |
| `src/dashboard.py` | MODIFY — show agent name in task list |
| `src/dashboard_sse.py` | MODIFY — include agent in task/stats SSE payloads |
| `Dockerfile` | MODIFY — install kimi-cli and codex |
| `docker-compose.yml` | MODIFY — add KIMI_API_KEY, CODEX_API_KEY, BACKPORCHER_ENABLED_AGENTS env vars |
| `.env.example` | NEW — document required env vars |

**Not modified:** `src/review.py` — reviews stay Claude-only per Decision #1. No code changes needed.

## Decisions

1. **Reviews stay Claude-only initially** — the coordinator review is a critical quality gate. Keep it on Claude (the most capable) until we have data on other agents' review quality.
2. **Triage stays on Claude Haiku** — it's cheap, fast, and already works. Adding agent selection to its output is a prompt change, not an architecture change.
3. **Navigation context is agent-agnostic** — the Tree-sitter graph and navigation prompt work regardless of which agent executes the task.
4. **No shared agent state** — each backend is stateless. No session resumption across agents.

## Testing Strategy

1. Unit tests for each backend's `build_command()` and `parse_output_line()`
2. Integration test: run each agent on a trivial task (add a comment to a file) in a test repo
3. Triage test: verify the updated prompt correctly outputs agent selection
4. Fallback test: mock agent failure, verify re-queue to next agent
5. DB migration test: verify v8→v9 migration preserves existing data
