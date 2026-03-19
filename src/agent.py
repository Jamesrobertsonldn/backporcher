"""Agent execution: prompt building, agent runner, build verification."""

import asyncio
import json
import logging
import os
import signal
from pathlib import Path

from .config import Config
from .constants import (
    MAX_OUTPUT_BYTES,
    READLINE_LIMIT,
    SENSITIVE_ENV_VARS,
    TIMEOUT_VERIFY_AGENT,
    TRUNCATE_LOG_LINE,
    TRUNCATE_OUTPUT_TAIL,
    TRUNCATE_SUMMARY,
    TRUNCATE_VERIFY_OUTPUT,
    prlimit_args,
)
from .db import Database
from .navigation import generate_navigation_context
from .prompts import AGENT_PROMPT_TEMPLATE
from .repo_intel import get_learnings_text

log = logging.getLogger("backporcher.agent")


async def run_agent(
    task: dict,
    worktree_path: Path,
    config: Config,
    db: Database,
) -> tuple[int, str | None]:
    """
    Run claude -p in the worktree. Streams stdout to log file.
    Returns (exit_code, output_summary).
    Uses Max subscription -- no --max-budget-usd flag.
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
