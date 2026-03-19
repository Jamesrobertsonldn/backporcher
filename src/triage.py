"""Triage: issue classification, batch orchestration, conflict detection."""

import asyncio
import json
import logging
import os

from .config import Config
from .constants import (
    SENSITIVE_ENV_VARS,
    TIMEOUT_BATCH_ORCHESTRATION,
    TIMEOUT_CONFLICT_CHECK,
    TIMEOUT_TRIAGE_MODEL,
    TRUNCATE_BATCH_ISSUE_BODY,
    TRUNCATE_PROMPT_FOR_REVIEW,
    TRUNCATE_REASON,
    TRUNCATE_TRIAGE_BODY,
    prlimit_args,
)
from .prompts import (
    BATCH_ORCHESTRATE_PROMPT_TEMPLATE,
    CONFLICT_CHECK_PROMPT_TEMPLATE,
    TRIAGE_PROMPT_TEMPLATE,
)

log = logging.getLogger("backporcher.triage")


async def triage_issue(title: str, body: str, config: Config) -> tuple[str, str]:
    """Run haiku to classify issue complexity. Returns (model, reason)."""
    prompt = TRIAGE_PROMPT_TEMPLATE.format(
        title=title,
        body=(body or "(no body)")[:TRUNCATE_TRIAGE_BODY],
    )

    cmd = ["claude", "-p", "--output-format", "text", "--model", "haiku", prompt]

    if config.agent_user:
        cmd = [
            "sudo",
            "-u",
            config.agent_user,
            "--",
            *prlimit_args(),
            *cmd,
        ]
        agent_env = None
    else:
        _sensitive_vars = SENSITIVE_ENV_VARS | {
            "CLAUDECODE",
            "SSH_AUTH_SOCK",
            "SSH_AGENT_PID",
            "GIT_ASKPASS",
            "GIT_CREDENTIALS",
        }
        agent_env = {k: v for k, v in os.environ.items() if k not in _sensitive_vars}

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        **({"env": agent_env} if agent_env is not None else {}),
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_TRIAGE_MODEL)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("Triage timed out, defaulting to %s", config.default_model)
        return config.default_model, "triage timed out"

    output = stdout.decode(errors="replace").strip()
    if proc.returncode != 0:
        log.warning("Triage failed (exit %d), defaulting to %s", proc.returncode, config.default_model)
        return config.default_model, f"triage failed (exit {proc.returncode})"

    # Parse "MODEL: opus — reason" or "MODEL: sonnet — reason"
    for line in output.strip().splitlines():
        cleaned = line.strip().strip("*_").strip()
        upper = cleaned.upper()
        if upper.startswith("MODEL: OPUS"):
            reason = (
                cleaned.split("—", 1)[-1].strip()
                if "—" in cleaned
                else cleaned.split("-", 1)[-1].strip()
                if "- " in cleaned
                else "classified as complex"
            )
            return "opus", reason
        elif upper.startswith("MODEL: SONNET"):
            reason = (
                cleaned.split("—", 1)[-1].strip()
                if "—" in cleaned
                else cleaned.split("-", 1)[-1].strip()
                if "- " in cleaned
                else "classified as straightforward"
            )
            return "sonnet", reason

    log.warning("Could not parse triage output, defaulting to %s: %s", config.default_model, output[:TRUNCATE_REASON])
    return config.default_model, "unparseable triage output"


async def orchestrate_batch(
    issues: list[dict],
    repo_name: str,
    config: Config,
) -> list[dict] | None:
    """Batch-orchestrate multiple issues via haiku. Returns list of dicts with
    issue_number, model, priority, depends_on, reason. Returns None on failure."""
    issues_lines = []
    for iss in issues:
        body = (iss.get("body") or "(no body)")[:TRUNCATE_BATCH_ISSUE_BODY]
        issues_lines.append(f"### Issue #{iss['number']}: {iss['title']}\n{body}\n")

    prompt = BATCH_ORCHESTRATE_PROMPT_TEMPLATE.format(
        repo_name=repo_name,
        issues_block="\n".join(issues_lines),
        n_issues=len(issues),
    )

    cmd = ["claude", "-p", "--output-format", "text", "--model", "haiku", prompt]

    if config.agent_user:
        cmd = [
            "sudo",
            "-u",
            config.agent_user,
            "--",
            *prlimit_args(),
            *cmd,
        ]
        agent_env = None
    else:
        _sensitive_vars = SENSITIVE_ENV_VARS | {
            "CLAUDECODE",
            "SSH_AUTH_SOCK",
            "SSH_AGENT_PID",
            "GIT_ASKPASS",
            "GIT_CREDENTIALS",
        }
        agent_env = {k: v for k, v in os.environ.items() if k not in _sensitive_vars}

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        **({"env": agent_env} if agent_env is not None else {}),
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_BATCH_ORCHESTRATION)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("Batch orchestration timed out")
        return None

    output = stdout.decode(errors="replace").strip()
    if proc.returncode != 0:
        log.warning(
            "Batch orchestration failed (exit %d): %s",
            proc.returncode,
            stderr.decode(errors="replace")[:TRUNCATE_REASON],
        )
        return None

    # Strip markdown fences if present
    cleaned = output
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        # Remove first and last fence lines
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("Batch orchestration returned invalid JSON: %s", cleaned[:TRUNCATE_REASON])
        return None

    if not isinstance(result, list):
        log.warning("Batch orchestration returned non-list: %s", type(result))
        return None

    # Validate entries
    issue_numbers = {iss["number"] for iss in issues}
    valid_models = {"sonnet", "opus"}
    validated = []

    for entry in result:
        num = entry.get("issue_number")
        if num not in issue_numbers:
            continue
        model = entry.get("model", config.default_model)
        if model not in valid_models:
            model = config.default_model
        priority = entry.get("priority", 100)
        if not isinstance(priority, int):
            priority = 100
        depends_on = entry.get("depends_on")
        if depends_on is not None and depends_on not in issue_numbers:
            depends_on = None
        reason = entry.get("reason", "")
        validated.append(
            {
                "issue_number": num,
                "model": model,
                "priority": priority,
                "depends_on": depends_on,
                "reason": str(reason)[:TRUNCATE_REASON],
            }
        )

    # Fill in any issues the orchestrator omitted
    seen_numbers = {e["issue_number"] for e in validated}
    for iss in issues:
        if iss["number"] not in seen_numbers:
            validated.append(
                {
                    "issue_number": iss["number"],
                    "model": config.default_model,
                    "priority": 100,
                    "depends_on": None,
                    "reason": "omitted by orchestrator, using defaults",
                }
            )

    return validated


async def check_task_conflict(
    task_prompt: str,
    inflight_tasks: list[dict],
    config: Config,
) -> dict | None:
    """Check if a new task conflicts with in-flight tasks. Returns conflict info or None.

    Calls haiku with a focused prompt. Fail-open: returns None on any error.
    """
    if not inflight_tasks:
        return None

    summaries = []
    for t in inflight_tasks:
        summaries.append(f"- Task #{t['id']} ({t['status']}): {t['prompt'][:TRUNCATE_REASON]}")
    inflight_text = "\n".join(summaries)

    prompt = CONFLICT_CHECK_PROMPT_TEMPLATE.format(
        new_task_prompt=task_prompt[:TRUNCATE_PROMPT_FOR_REVIEW],
        inflight_summaries=inflight_text,
    )

    cmd = ["claude", "-p", "--output-format", "text", "--model", "haiku", prompt]

    if config.agent_user:
        cmd = [
            "sudo",
            "-u",
            config.agent_user,
            "--",
            *prlimit_args(),
            *cmd,
        ]
        agent_env = None
    else:
        _sensitive_vars = SENSITIVE_ENV_VARS | {
            "CLAUDECODE",
            "SSH_AUTH_SOCK",
            "SSH_AGENT_PID",
            "GIT_ASKPASS",
            "GIT_CREDENTIALS",
        }
        agent_env = {k: v for k, v in os.environ.items() if k not in _sensitive_vars}

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        **({"env": agent_env} if agent_env is not None else {}),
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_CONFLICT_CHECK)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("Conflict check timed out, proceeding without blocking")
        return None

    output = stdout.decode(errors="replace").strip()
    if proc.returncode != 0:
        log.warning("Conflict check failed (exit %d), proceeding", proc.returncode)
        return None

    # Strip markdown fences if present
    cleaned = output
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("Conflict check returned invalid JSON: %s", cleaned[:TRUNCATE_REASON])
        return None

    if not isinstance(result, dict):
        return None

    if result.get("conflict"):
        return result
    return None
