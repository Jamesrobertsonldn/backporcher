"""Dispatcher: task dispatch lifecycle, credential sync, retry logic."""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .constants import (
    CREDENTIAL_FILE_MODE,
    TIMEOUT_COMMIT_PUSH,
    TRUNCATE_COMMIT_MSG,
    TRUNCATE_ERROR_MESSAGE,
    TRUNCATE_REASON,
    TRUNCATE_REVIEW_OUTPUT,
    TRUNCATE_SUMMARY,
)
from .db import Database
from .github import (
    comment_on_issue,
    repo_full_name_from_url,
    update_issue_labels,
)

# Import from split modules
from .git_ops import (
    _get_repo_lock,
    cleanup_task_artifacts,
    clone_or_fetch,
    ensure_repo_permissions,
    make_branch_name,
    run_cmd,
    setup_worktree,
    validate_github_url,
    repo_name_from_url,
)
from .agent import (
    AGENT_PROMPT_TEMPLATE,
    detect_and_store_stack,
    detect_stack,
    generate_navigation_context,
    get_learnings_text,
    record_learning,
    run_agent,
    run_verify,
)
from .review import create_pr, run_review
from .triage import (
    check_task_conflict,
    orchestrate_batch,
    triage_issue,
)

log = logging.getLogger("backporcher.dispatcher")


async def _mark_issue_failed(task: dict, db: Database, reason: str):
    """Update GitHub labels on the source issue when a task permanently fails.

    Moves from backporcher-in-progress → backporcher-failed and posts a comment.
    No-op if the task didn't originate from a GitHub issue.
    """
    issue_num = task.get("github_issue_number")
    if not issue_num:
        return
    repo = await db.get_repo(task["repo_id"])
    if not repo:
        return
    repo_full = repo_full_name_from_url(repo["github_url"])
    await update_issue_labels(
        repo_full,
        issue_num,
        add=["backporcher-failed"],
        remove=["backporcher-in-progress"],
    )
    await comment_on_issue(
        repo_full,
        issue_num,
        f"{reason}\n\nRe-add the `backporcher` label to retry.",
    )


async def sync_agent_credentials(config: Config):
    """Copy admin's Claude credentials to agent user if they're newer."""
    if not config.agent_user:
        return
    admin_cred = Path.home() / ".claude" / ".credentials.json"
    agent_cred = Path(f"/home/{config.agent_user}") / ".claude" / ".credentials.json"
    if not admin_cred.exists():
        return
    # Use sudo stat to check agent cred mtime (file is 600 owned by agent user)
    need_sync = True
    rc, out, _ = await run_cmd("sudo", "stat", "-c", "%Y", str(agent_cred))
    if rc == 0:
        try:
            agent_mtime = float(out.strip())
            need_sync = admin_cred.stat().st_mtime > agent_mtime
        except (ValueError, OSError):
            pass

    if need_sync:
        log.info("Syncing Claude credentials to %s", config.agent_user)
        rc, _, err = await run_cmd(
            "sudo",
            "install",
            "-m",
            f"{CREDENTIAL_FILE_MODE:o}",
            "-o",
            config.agent_user,
            "-g",
            "backporcher",
            str(admin_cred),
            str(agent_cred),
        )
        if rc != 0:
            log.warning("Failed to sync credentials: %s", err.strip())


def _pick_retry_model(current_model: str, retry_count: int) -> str:
    """Escalate model on retry. Sonnet -> opus after first attempt."""
    if current_model == "sonnet" and retry_count >= 1:
        log.info("Model escalation: sonnet -> opus (retry %d)", retry_count)
        return "opus"
    return current_model


async def retry_with_ci_context(
    task: dict,
    ci_logs: str,
    config: Config,
    db: Database,
):
    """Re-run the agent with CI failure context on the existing branch."""
    task_id = task["id"]
    repo = await db.get_repo(task["repo_id"])
    if not repo:
        raise ValueError(f"Repo {task['repo_id']} not found")

    worktree_path = Path(task["worktree_path"])
    if not worktree_path.exists():
        raise RuntimeError(f"Worktree missing for task {task_id}: {worktree_path}")

    # Pull latest on the branch
    repo_lock = _get_repo_lock(repo["id"])
    async with repo_lock:
        rc, _, err = await run_cmd("git", "pull", "--rebase", cwd=worktree_path)
        if rc != 0:
            log.warning("git pull failed for retry task %d: %s", task_id, err)

    # Build augmented prompt
    augmented_prompt = (
        f"{task['prompt']}\n\n"
        f"---\n"
        f"IMPORTANT: The previous attempt created a PR but CI checks failed. "
        f"Please fix the issues shown in the CI logs below and commit the fixes.\n\n"
        f"CI FAILURE LOGS:\n```\n{ci_logs}\n```"
    )

    # Temporarily patch the task's prompt for the agent run
    patched_task = dict(task)
    patched_task["prompt"] = augmented_prompt

    await db.add_log(task_id, f"Retry #{task['retry_count']}: running agent with CI context")
    exit_code, summary = await run_agent(patched_task, worktree_path, config, db)

    if exit_code != 0:
        now = datetime.now(timezone.utc).isoformat()
        await db.update_task(
            task_id,
            status="failed",
            error_message=f"Retry agent exited with code {exit_code}",
            completed_at=now,
        )
        await _mark_issue_failed(
            task,
            db,
            f"CI retry agent failed with exit code {exit_code}.",
        )
        await cleanup_task_artifacts(task, db)
        return

    # Push fixes (force-with-lease since we're updating the same branch)
    branch = task["branch_name"]
    rc, _, err = await run_cmd(
        "git",
        "push",
        "--force-with-lease",
        "origin",
        branch,
        cwd=worktree_path,
        timeout=TIMEOUT_COMMIT_PUSH,
    )
    if rc != 0:
        await db.add_log(task_id, f"Force push failed on retry: {err}", level="error")
        raise RuntimeError(f"git push failed on retry: {err}")

    # Back to pr_created — CI monitor will check again
    await db.update_task(task_id, status="pr_created")
    await db.add_log(task_id, f"Retry #{task['retry_count']}: pushed fixes, awaiting CI")


async def dispatch_task(task: dict, config: Config, db: Database):
    """Full lifecycle: fetch → worktree → agent → PR."""
    task_id = task["id"]
    try:
        repo = await db.get_repo(task["repo_id"])
        if not repo:
            raise ValueError(f"Repo {task['repo_id']} not found")

        # Serialize git operations per-repo (fetch + worktree creation)
        repo_lock = _get_repo_lock(repo["id"])
        async with repo_lock:
            await db.add_log(task_id, "Fetching repository...")
            repo_path = await clone_or_fetch(repo, config)
            await ensure_repo_permissions(repo_path, config)
            await detect_and_store_stack(repo, db)

            branch = make_branch_name(task_id, task["prompt"])
            await db.update_task(task_id, branch_name=branch)
            await db.add_log(task_id, f"Creating worktree on branch {branch}")
            worktree_path = await setup_worktree(
                repo_path,
                task_id,
                branch,
                repo["default_branch"],
            )
            await db.update_task(task_id, worktree_path=str(worktree_path))

        # Ensure agent credentials are fresh before launching
        await sync_agent_credentials(config)

        # Record agent start timing and model info
        agent_start_now = datetime.now(timezone.utc).isoformat()
        await db.update_task(
            task_id,
            agent_started_at=agent_start_now,
            initial_model=task["model"],
            model_used=task["model"],
        )
        await db.record_metric(
            "agent_start",
            task_id=task_id,
            repo=task.get("repo_name"),
            model=task["model"],
        )

        # Run agent
        await db.add_log(task_id, "Running agent...")
        exit_code, summary = await run_agent(task, worktree_path, config, db)

        # Record agent finish timing
        agent_finish_now = datetime.now(timezone.utc).isoformat()
        await db.update_task(
            task_id,
            exit_code=exit_code,
            output_summary=summary[:TRUNCATE_REVIEW_OUTPUT] if summary else None,
            agent_finished_at=agent_finish_now,
            model_used=task["model"],
        )

        if exit_code != 0:
            retry_count = task.get("retry_count", 0)
            max_retries = config.max_task_retries

            if retry_count < max_retries:
                new_count = retry_count + 1
                new_model = _pick_retry_model(task["model"], new_count)
                await db.update_task(
                    task_id,
                    status="queued",
                    error_message=None,
                    started_at=None,
                    branch_name=None,
                    worktree_path=None,
                    retry_count=new_count,
                    model=new_model,
                )
                reason = f"exit {exit_code}"
                await db.add_log(
                    task_id,
                    f"Agent failed ({reason}), retry {new_count}/{max_retries} (model={new_model})",
                    level="warn",
                )
                log.info(
                    "Task %d: agent failed (%s), retry %d/%d (model=%s)",
                    task_id,
                    reason,
                    new_count,
                    max_retries,
                    new_model,
                )
                await db.record_metric(
                    "retry_agent",
                    task_id=task_id,
                    repo=task.get("repo_name"),
                    model=new_model,
                )
                return

            # Max retries exhausted — permanent failure
            now = datetime.now(timezone.utc).isoformat()
            await db.update_task(
                task_id,
                status="failed",
                error_message=f"Agent exited with code {exit_code} (retries exhausted)",
                completed_at=now,
            )
            await db.add_log(task_id, f"Agent failed (exit {exit_code}), retries exhausted", level="error")
            await record_learning(
                db,
                task["repo_id"],
                task_id,
                "agent_failure",
                f"Agent failed (exit {exit_code}) on: {task['prompt'][:TRUNCATE_REASON]}",
            )
            await _mark_issue_failed(
                task,
                db,
                f"Agent failed with exit code {exit_code} (retries exhausted).",
            )
            await cleanup_task_artifacts(task, db)
            cascaded = await db.handle_dependency_failure(task_id)
            if cascaded:
                log.info("Task #%d failure cascaded to tasks: %s", task_id, cascaded)
            return

        # Build verification loop
        verify_command = repo.get("verify_command")
        if verify_command:
            for attempt in range(1, config.max_verify_retries + 2):  # +2: initial + retries
                passed, verify_output = await run_verify(
                    worktree_path,
                    verify_command,
                    task_id,
                    db,
                    config,
                )
                if passed:
                    break

                if attempt > config.max_verify_retries:
                    now = datetime.now(timezone.utc).isoformat()
                    await db.update_task(
                        task_id,
                        status="failed",
                        error_message=f"Verify failed after {config.max_verify_retries} fix attempts",
                        completed_at=now,
                    )
                    await db.add_log(
                        task_id,
                        f"Verify failed after {config.max_verify_retries} retries, giving up",
                        level="error",
                    )
                    await record_learning(
                        db,
                        task["repo_id"],
                        task_id,
                        "verify_failure",
                        f"Build verification failed ({verify_command}) on: {task['prompt'][:TRUNCATE_REASON]}",
                    )
                    await _mark_issue_failed(
                        task,
                        db,
                        f"Build verification failed after {config.max_verify_retries} fix attempts.",
                    )
                    await cleanup_task_artifacts(task, db)
                    cascaded = await db.handle_dependency_failure(task_id)
                    if cascaded:
                        log.info("Task #%d failure cascaded to tasks: %s", task_id, cascaded)
                    return

                # Re-run agent with verify failure context
                await db.add_log(
                    task_id,
                    f"Verify fix attempt {attempt}/{config.max_verify_retries}",
                )
                fix_prompt = (
                    f"{task['prompt']}\n\n"
                    f"---\n"
                    f"IMPORTANT: Your previous changes failed the build/test verification.\n"
                    f"The verify command `{verify_command}` failed with this output:\n\n"
                    f"```\n{verify_output}\n```\n\n"
                    f"Fix the errors and make sure the build passes."
                )
                fix_task = dict(task)
                fix_task["prompt"] = fix_prompt
                exit_code, summary = await run_agent(fix_task, worktree_path, config, db)
                if exit_code != 0:
                    # Let the outer exception handler deal with retry/escalation
                    raise RuntimeError(f"Verify fix agent exited with code {exit_code}")

        # Create PR
        await db.add_log(task_id, "Creating pull request...")
        # Re-read task to get branch_name
        task = await db.get_task(task_id)
        pr_url = await create_pr(worktree_path, task, repo, db)
        now = datetime.now(timezone.utc).isoformat()

        if pr_url:
            await db.update_task(
                task_id,
                status="pr_created",
                pr_url=pr_url,
                completed_at=now,
            )
            await db.add_log(task_id, f"PR created: {pr_url}")

            # Comment on the GitHub issue if this task came from one
            issue_num = task.get("github_issue_number")
            if issue_num:
                repo_full = repo_full_name_from_url(repo["github_url"])
                await comment_on_issue(
                    repo_full,
                    issue_num,
                    f"PR created: {pr_url}\n\nAwaiting CI checks.",
                )
        else:
            await db.update_task(
                task_id,
                status="completed",
                completed_at=now,
            )
            await db.add_log(task_id, "Completed (no changes)")
            await cleanup_task_artifacts(task, db)

    except Exception as e:
        log.exception("Task %d failed", task_id)
        err_str = str(e)[:TRUNCATE_ERROR_MESSAGE]
        now = datetime.now(timezone.utc).isoformat()

        retry_count = task.get("retry_count", 0)
        if retry_count < config.max_task_retries:
            new_count = retry_count + 1
            new_model = _pick_retry_model(task.get("model", "sonnet"), new_count)
            await db.update_task(
                task_id,
                status="queued",
                error_message=None,
                started_at=None,
                branch_name=None,
                worktree_path=None,
                retry_count=new_count,
                model=new_model,
            )
            await db.add_log(
                task_id,
                f"Error, retry {new_count}/{config.max_task_retries} (model={new_model}): {err_str[:TRUNCATE_REASON]}",
                level="warn",
            )
            log.info(
                "Task %d: error, retry %d/%d (model=%s)",
                task_id,
                new_count,
                config.max_task_retries,
                new_model,
            )
            await db.record_metric(
                "retry_agent",
                task_id=task_id,
                repo=task.get("repo_name"),
                model=new_model,
            )
        else:
            await db.update_task(
                task_id,
                status="failed",
                error_message=err_str,
                completed_at=now,
            )
            await db.add_log(task_id, f"Fatal error (retries exhausted): {e}", level="error")
            await _mark_issue_failed(
                task,
                db,
                f"Task failed with error: {err_str[:TRUNCATE_REASON]}",
            )
            await cleanup_task_artifacts(task, db)
            cascaded = await db.handle_dependency_failure(task_id)
            if cascaded:
                log.info("Task #%d failure cascaded to tasks: %s", task_id, cascaded)
