"""Agent execution: prompt building, stack detection, learning loop, navigation context."""

import asyncio
import json
import logging
import os
import signal
from pathlib import Path

from .config import Config
from .constants import (
    MAX_OUTPUT_BYTES,
    NAV_MAX_EDGES,
    NAV_MAX_FILES,
    NAV_MAX_SYMBOLS_PER_FILE,
    READLINE_LIMIT,
    SENSITIVE_ENV_VARS,
    TIMEOUT_NAVIGATION_MODEL,
    TIMEOUT_VERIFY_AGENT,
    TRUNCATE_LOG_LINE,
    TRUNCATE_NAV_CONTEXT,
    TRUNCATE_OUTPUT_TAIL,
    TRUNCATE_SUMMARY,
    TRUNCATE_VERIFY_OUTPUT,
    prlimit_args,
)
from .db import Database

log = logging.getLogger("backporcher.agent")

AGENT_PROMPT_TEMPLATE = """\
IMPORTANT: You are running non-interactively via an automated dispatcher.
Implement directly — do NOT give an approach summary or wait for approval.

{project_context}{learnings_section}{navigation_section}## Task
{task_prompt}

## Execution Guidelines
1. Identify which files need changes before writing code
2. Run existing tests after your changes to verify nothing breaks
3. If you get stuck, commit what you have and document what remains in a TODO comment
4. Keep changes focused — don't refactor unrelated code
"""

NAVIGATION_PROMPT = """\
You are a code navigation assistant. Given a task description and a dependency graph excerpt from the codebase, select the 5-15 most relevant files the developer should examine first to complete the task.

For each file, list the key symbols (functions/classes) and a one-line rationale explaining why it's relevant.

Output ONLY a JSON array, no markdown fences:
[{"file": "relative/path.py", "symbols": ["func_name", "ClassName"], "why": "one-line rationale"}]

## Task
{task_prompt}

## Graph Data
### Directly Matched Files
{matched_files}

### Related Files (1-hop dependencies)
{related_files}

### Key Dependency Edges
{edges}
"""


def detect_stack(repo_path: Path) -> str:
    """Detect tech stack from project files. Returns a summary string."""
    parts = []

    # Language detection
    pyproject = repo_path / "pyproject.toml"
    package_json = repo_path / "package.json"
    cargo_toml = repo_path / "Cargo.toml"
    go_mod = repo_path / "go.mod"
    gemfile = repo_path / "Gemfile"

    if pyproject.exists():
        parts.append("Python")
        try:
            content = pyproject.read_text(errors="replace")
            if "django" in content.lower():
                parts.append("Django")
            elif "fastapi" in content.lower():
                parts.append("FastAPI")
            elif "flask" in content.lower():
                parts.append("Flask")
            if "alembic" in content.lower():
                parts.append("Alembic")
            if "pytest" in content.lower():
                parts.append("pytest")
        except OSError:
            pass
    elif (repo_path / "requirements.txt").exists() or (repo_path / "setup.py").exists():
        parts.append("Python")

    if package_json.exists():
        try:
            pkg = json.loads(package_json.read_text(errors="replace"))
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in deps:
                version = deps["next"].lstrip("^~>=<")
                major = version.split(".")[0] if version[0].isdigit() else ""
                parts.append(f"Next.js {major}" if major else "Next.js")
            elif "react" in deps:
                parts.append("React")
            elif "vue" in deps:
                parts.append("Vue")
            elif "svelte" in deps or "@sveltejs/kit" in deps:
                parts.append("Svelte")
            else:
                parts.append("Node.js")
            if "typescript" in deps:
                parts.append("TypeScript")
            if "@prisma/client" in deps or "prisma" in deps:
                parts.append("Prisma")
            if "jest" in deps or "@jest/core" in deps:
                parts.append("Jest")
            elif "vitest" in deps:
                parts.append("Vitest")
        except (json.JSONDecodeError, KeyError, TypeError, IndexError, OSError):
            parts.append("Node.js")

    if cargo_toml.exists():
        parts.append("Rust")
        try:
            content = cargo_toml.read_text(errors="replace")
            if "tauri" in content.lower():
                parts.append("Tauri")
        except OSError:
            pass

    if go_mod.exists():
        parts.append("Go")

    if gemfile.exists():
        parts.append("Ruby")
        try:
            content = gemfile.read_text(errors="replace")
            if "rails" in content.lower():
                parts.append("Rails")
        except OSError:
            pass

    # Infra
    if (repo_path / "Dockerfile").exists() or (repo_path / "docker-compose.yml").exists():
        parts.append("Docker")
    if (repo_path / ".github" / "workflows").is_dir():
        parts.append("GitHub Actions")

    return " + ".join(parts) if parts else "Unknown"


async def detect_and_store_stack(repo: dict, db: Database):
    """Detect stack if not already stored."""
    if repo.get("stack_info"):
        return
    repo_path = Path(repo["local_path"])
    if not repo_path.exists():
        return
    stack = detect_stack(repo_path)
    if stack and stack != "Unknown":
        await db.update_repo(repo["id"], stack_info=stack)
        log.info("Detected stack for %s: %s", repo["name"], stack)


async def record_learning(
    db: Database,
    repo_id: int,
    task_id: int | None,
    learning_type: str,
    context: str,
):
    """Extract a learning from context and store it."""
    # Take first meaningful line (skip empty/whitespace)
    content = ""
    for line in context.strip().splitlines():
        line = line.strip()
        if line:
            content = line[:TRUNCATE_SUMMARY]
            break
    if not content:
        content = context.strip()[:TRUNCATE_SUMMARY]
    if not content:
        return
    try:
        await db.add_learning(repo_id, learning_type, content, task_id=task_id)
    except Exception:
        log.warning("Failed to record learning for repo %d", repo_id, exc_info=True)


async def get_learnings_text(db: Database, repo_id: int) -> str | None:
    """Format recent learnings for prompt injection."""
    learnings = await db.get_learnings(repo_id, limit=10)
    if not learnings:
        return None
    lines = []
    for entry in learnings:
        icon = {
            "success": "+",
            "agent_failure": "!",
            "verify_failure": "!",
            "ci_failure": "!",
            "coordinator_rejection": "!",
        }.get(entry["learning_type"], "-")
        lines.append(f"  [{icon}] {entry['content']}")
    return "## Learnings from Previous Tasks\n" + "\n".join(lines) + "\n\n"


async def generate_navigation_context(
    task: dict,
    repo_path: Path,
    db: Database,
    config: Config,
) -> str | None:
    """Use sonnet + code graph to build a navigation map for the work agent.

    Returns a formatted prompt section, or None on any failure.
    """
    if not config.navigation_enabled:
        return None

    try:
        from .graph import build_navigation_context, ensure_graph

        store = await ensure_graph(repo_path)
        if not store:
            return None

        # Run graph query in thread (CPU-bound)
        nav_data = await asyncio.to_thread(build_navigation_context, store, task["prompt"], repo_path)
        if not nav_data or not nav_data.get("matched_files"):
            return None

        # Format graph data for the navigation model
        matched_text = "\n".join(
            f"- {f['path']}: {', '.join(f['symbols'][:NAV_MAX_SYMBOLS_PER_FILE])} ({f['match_reason']})"
            for f in nav_data["matched_files"]
        )
        related_text = (
            "\n".join(
                f"- {f['path']}: {', '.join(f['symbols'][:NAV_MAX_SYMBOLS_PER_FILE])} (via {f['relationship']})"
                for f in nav_data["related_files"]
            )
            or "(none)"
        )
        edges_text = (
            "\n".join(f"- {e['from']} --[{e['kind']}]--> {e['to']}" for e in nav_data["edges"][:NAV_MAX_EDGES])
            or "(none)"
        )

        nav_prompt = NAVIGATION_PROMPT.format(
            task_prompt=task["prompt"],
            matched_files=matched_text,
            related_files=related_text,
            edges=edges_text,
        )

        # Call sonnet for navigation (single-shot, 60s timeout)
        cmd = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--model",
            config.navigation_model,
            nav_prompt,
        ]

        # Clean env (same as agent)
        nav_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(repo_path),
            env=nav_env,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_NAVIGATION_MODEL)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            log.warning("Navigation context timed out for task %d", task["id"])
            return None

        if proc.returncode != 0:
            log.warning("Navigation model failed (exit %d) for task %d", proc.returncode, task["id"])
            return None

        # Parse response — extract JSON from the output
        output = stdout.decode(errors="replace").strip()

        # claude --output-format json wraps result in {"type":"result","result":"..."}
        try:
            wrapper = json.loads(output)
            if isinstance(wrapper, dict) and "result" in wrapper:
                output = wrapper["result"]
        except json.JSONDecodeError:
            pass

        # Strip markdown fences if model wrapped them
        if output.startswith("```"):
            lines = output.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            output = "\n".join(lines)

        try:
            files = json.loads(output)
        except json.JSONDecodeError:
            log.warning("Navigation model returned invalid JSON for task %d", task["id"])
            return None

        if not isinstance(files, list) or not files:
            return None

        # Format into prompt section (capped at ~4k chars)
        section_lines = [
            "## Navigation Context",
            "These files are most relevant to your task (from dependency analysis):",
        ]
        total_len = sum(len(line) for line in section_lines)
        for entry in files[:NAV_MAX_FILES]:
            if not isinstance(entry, dict):
                continue
            fpath = entry.get("file", "")
            symbols = entry.get("symbols", [])
            why = entry.get("why", "")
            sym_str = ", ".join(str(s) for s in symbols[:NAV_MAX_SYMBOLS_PER_FILE]) if symbols else ""
            line = f"  - {fpath}"
            if sym_str:
                line += f" — {sym_str}"
            if why:
                line += f"\n    Why: {why}"
            if total_len + len(line) > TRUNCATE_NAV_CONTEXT:
                break
            section_lines.append(line)
            total_len += len(line)

        if len(section_lines) <= 2:
            return None  # No files made it through

        return "\n".join(section_lines) + "\n\n"

    except Exception:
        log.debug("Failed to generate navigation context for task %d", task["id"], exc_info=True)
        return None


async def run_agent(
    task: dict,
    worktree_path: Path,
    config: Config,
    db: Database,
) -> tuple[int, str | None]:
    """
    Run claude -p in the worktree. Streams stdout to log file.
    Returns (exit_code, output_summary).
    Uses Max subscription — no --max-budget-usd flag.
    """
    # Build structured prompt with stack info, learnings, and navigation context
    project_context = ""
    learnings_section = ""
    navigation_section = ""
    try:
        repo = await db.get_repo(task["repo_id"])
        if repo:
            stack = repo.get("stack_info")
            if stack:
                project_context = f"## Project Context\nTech stack: {stack}\n\n"
            learnings_section = await get_learnings_text(db, task["repo_id"]) or ""
            # Generate navigation context from code graph
            repo_path = Path(repo["local_path"])
            if repo_path.exists():
                nav = await generate_navigation_context(task, repo_path, db, config)
                if nav:
                    navigation_section = nav
                    await db.add_log(task["id"], "Navigation context generated from code graph")
    except Exception:
        log.debug("Failed to fetch context for prompt", exc_info=True)

    prompt = AGENT_PROMPT_TEMPLATE.format(
        project_context=project_context,
        learnings_section=learnings_section,
        navigation_section=navigation_section,
        task_prompt=task["prompt"],
    )
    model = task["model"]
    log_file = config.logs_dir / f"{task['id']}.jsonl"

    cmd = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--model",
        model,
        prompt,
    ]

    # Sandbox: wrap with sudo -u + prlimit when agent_user is configured
    if config.agent_user:
        cmd = [
            "sudo",
            "-u",
            config.agent_user,
            "--",
            *prlimit_args(),
            *cmd,
        ]
        agent_env = None  # Let sudo reset env to target user's defaults
    else:
        # Clean env: strip sensitive vars and CLAUDECODE (nested-session detection)
        _sensitive_vars = SENSITIVE_ENV_VARS | {
            "CLAUDECODE",
            "SSH_AUTH_SOCK",
            "SSH_AGENT_PID",
            "GIT_ASKPASS",
            "GIT_CREDENTIALS",
        }
        agent_env = {k: v for k, v in os.environ.items() if k not in _sensitive_vars}

    log.info("Starting agent for task %d (model=%s, user=%s)", task["id"], model, config.agent_user or "self")
    await db.add_log(task["id"], f"Starting agent with model={model}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(worktree_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=READLINE_LIMIT,
        **({"env": agent_env} if agent_env is not None else {}),
    )

    await db.update_task(task["id"], agent_pid=proc.pid)

    output_summary = None
    last_content: list[str] = []
    content_size = 0

    async def read_stream():
        nonlocal output_summary, content_size
        fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        with os.fdopen(fd, "w") as lf:
            async for raw_line in proc.stdout:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue

                # Write every line to the log file
                lf.write(line + "\n")
                lf.flush()

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")

                if etype == "assistant" and "message" in event:
                    msg = event["message"]
                    for block in msg.get("content") or []:
                        if block.get("type") == "text":
                            text = block["text"]
                            if content_size < MAX_OUTPUT_BYTES:
                                last_content.append(text)
                                content_size += len(text)

                elif etype == "result":
                    output_summary = event.get("result", "")
                    if event.get("is_error"):
                        await db.add_log(
                            task["id"],
                            f"Agent error: {output_summary[:TRUNCATE_SUMMARY]}",
                            level="error",
                        )

                elif etype == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if content_size < MAX_OUTPUT_BYTES:
                            last_content.append(text)
                            content_size += len(text)

    async def read_stderr():
        async for raw_line in proc.stderr:
            line = raw_line.decode(errors="replace").strip()
            if line:
                await db.add_log(task["id"], f"stderr: {line[:TRUNCATE_LOG_LINE]}", level="warn")

    try:
        await asyncio.wait_for(
            asyncio.gather(read_stream(), read_stderr()),
            timeout=config.task_timeout_seconds,
        )
        await proc.wait()
    except asyncio.TimeoutError:
        log.warning("Task %d timed out after %ds", task["id"], config.task_timeout_seconds)
        await db.add_log(
            task["id"],
            f"TIMEOUT after {config.task_timeout_seconds}s",
            level="error",
        )
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            await asyncio.sleep(5)
            if proc.returncode is None:
                os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()

    if not output_summary and last_content:
        output_summary = "".join(last_content)[-TRUNCATE_OUTPUT_TAIL:]

    await db.add_log(
        task["id"],
        f"Agent exited with code {proc.returncode}",
    )

    return proc.returncode, output_summary


async def run_verify(
    worktree_path: Path,
    verify_command: str,
    task_id: int,
    db: Database,
    config: Config | None = None,
) -> tuple[bool, str]:
    """Run repo's verify command in the worktree. Returns (passed, output)."""
    log.info("Task #%d: running verify: %s", task_id, verify_command)
    await db.add_log(task_id, f"Running verify: {verify_command}")

    # Run as agent user when sandboxing is configured, so target/ dirs
    # are owned by the same user that runs the agent
    cmd: list[str] = ["bash", "-c", verify_command]
    if config and config.agent_user:
        cmd = [
            "sudo",
            "-u",
            config.agent_user,
            "--",
            *prlimit_args(),
            *cmd,
        ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(worktree_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_VERIFY_AGENT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"Verify command timed out after {TIMEOUT_VERIFY_AGENT}s"

    output = stdout.decode(errors="replace")
    if proc.returncode == 0:
        await db.add_log(task_id, "Verify passed")
        return True, output

    # Truncate to last 3000 chars (most relevant part)
    if len(output) > TRUNCATE_VERIFY_OUTPUT:
        output = "...(truncated)...\n" + output[-TRUNCATE_VERIFY_OUTPUT:]
    await db.add_log(task_id, f"Verify failed (exit {proc.returncode})", level="warn")
    return False, output
