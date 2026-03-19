"""Coordinator review loop body: review PRs, approve/reject with retries."""

import logging

from . import notifications
from .config import Config
from .db import Database
from .dispatcher import (
    _pick_retry_model,
    cleanup_task_artifacts,
    record_learning,
    run_review,
)
from .github import (
    close_pr,
    comment_on_issue,
    comment_on_pr,
    extract_pr_number_from_url,
    repo_full_name_from_url,
    update_issue_labels,
)

log = logging.getLogger("backporcher.worker")


async def review_pending_tasks(db: Database, config: Config, running: bool) -> None:
    """One iteration of coordinator review for all pending PRs."""
    pending = await db.list_pending_review()
    for task in pending:
        if not running:
            break

        task_id = task["id"]
        pr_number = task.get("pr_number")

        # Backfill pr_number from pr_url if missing
        if not pr_number and task.get("pr_url"):
            pr_number = extract_pr_number_from_url(task["pr_url"])
            if pr_number:
                await db.update_task(task_id, pr_number=pr_number)
                task["pr_number"] = pr_number
                log.info("Task #%d: backfilled pr_number=%d from URL", task_id, pr_number)

        if not pr_number:
            fresh = await db.get_task(task_id)
            if fresh and fresh.get("pr_number"):
                pr_number = fresh["pr_number"]
                task["pr_number"] = pr_number
            else:
                log.warning("Task #%d: no pr_number, skipping review this cycle", task_id)
                continue

        repo_full = repo_full_name_from_url(task["github_url"])

        await db.update_task(task_id, status="reviewing")
        await db.add_log(task_id, "Coordinator review started")
        log.info("Task #%d: starting coordinator review (PR #%d)", task_id, pr_number)

        try:
            verdict, summary = await run_review(task, config, db)
        except Exception as e:
            log.exception("Review failed for task %d", task_id)
            verdict, summary = "approve", f"Review error (auto-approved): {e}"

        await db.update_task(task_id, review_summary=summary[:4000])

        if verdict == "approve":
            await _handle_review_approve(db, task_id, pr_number, repo_full, summary)
        else:
            await _handle_review_reject(db, config, task, task_id, pr_number, repo_full, summary)


async def _handle_review_approve(db: Database, task_id: int, pr_number: int, repo_full: str, summary: str) -> None:
    """Handle an approved coordinator review."""
    await db.update_task(task_id, status="reviewed")
    await db.add_log(task_id, "Coordinator approved PR")
    log.info("Task #%d: coordinator APPROVED", task_id)

    short_summary = summary[:1500] if len(summary) > 1500 else summary
    await comment_on_pr(
        repo_full,
        pr_number,
        f"**Coordinator Review: APPROVED**\n\n{short_summary}",
    )


async def _handle_review_reject(
    db: Database,
    config: Config,
    task: dict,
    task_id: int,
    pr_number: int,
    repo_full: str,
    summary: str,
) -> None:
    """Handle a rejected coordinator review — retry or permanent failure."""
    log.warning("Task #%d: coordinator REJECTED", task_id)
    await db.add_log(task_id, f"Coordinator rejected PR: {summary[:200]}", level="warn")

    retry_count = task.get("retry_count", 0)

    if retry_count < config.max_task_retries:
        new_count = retry_count + 1
        new_model = _pick_retry_model(task.get("model", "sonnet"), new_count)

        reject_comment = (
            f"**Coordinator Review: REJECTED**\n\n{summary[:1500]}\n\n"
            f"Retrying with {new_model} model (attempt {new_count}/{config.max_task_retries})."
        )
        await close_pr(repo_full, pr_number, comment=reject_comment)

        rejection_context = (
            f"\n\n---\n"
            f"IMPORTANT: A previous attempt at this task was rejected during code review.\n"
            f"Reviewer feedback:\n\n{summary[:2000]}\n\n"
            f"Address ALL the reviewer's concerns in your implementation."
        )
        original_prompt = task["prompt"]
        if "\n\n---\nIMPORTANT: A previous attempt" in original_prompt:
            original_prompt = original_prompt.split("\n\n---\nIMPORTANT: A previous attempt")[0]
        new_prompt = original_prompt + rejection_context

        await db.update_task(
            task_id,
            status="queued",
            started_at=None,
            branch_name=None,
            worktree_path=None,
            pr_url=None,
            pr_number=None,
            review_summary=None,
            retry_count=new_count,
            model=new_model,
            prompt=new_prompt,
        )
        await db.add_log(
            task_id,
            f"Coordinator rejected, retry {new_count}/{config.max_task_retries} (model={new_model})",
            level="warn",
        )
        log.info(
            "Task #%d: coordinator rejected, retry %d/%d (model=%s)",
            task_id,
            new_count,
            config.max_task_retries,
            new_model,
        )

        issue_num = task.get("github_issue_number")
        if issue_num:
            await comment_on_issue(
                repo_full,
                issue_num,
                f"PR rejected by coordinator. Retrying with {new_model} model "
                f"({new_count}/{config.max_task_retries})...\n\n"
                f"Feedback: {summary[:300]}",
            )
    else:
        # Max retries exhausted — permanent failure
        reject_comment = (
            f"**Coordinator Review: REJECTED**\n\n{summary[:1500]}\n\n"
            f"PR closed by Backporcher coordinator (retries exhausted)."
        )
        await close_pr(repo_full, pr_number, comment=reject_comment)
        await db.update_task(
            task_id,
            status="failed",
            error_message=f"Coordinator rejected (retries exhausted): {summary[:500]}",
        )

        await record_learning(
            db,
            task["repo_id"],
            task_id,
            "coordinator_rejection",
            f"Coordinator rejected: {summary[:200]}",
        )

        cascaded = await db.handle_dependency_failure(task_id)
        if cascaded:
            log.info("Task #%d rejection cascaded to tasks: %s", task_id, cascaded)

        issue_num = task.get("github_issue_number")
        if issue_num:
            await update_issue_labels(
                repo_full,
                issue_num,
                add=["backporcher-failed"],
                remove=["backporcher-in-progress"],
            )
            await comment_on_issue(
                repo_full,
                issue_num,
                f"PR was rejected by coordinator review (retries exhausted):\n\n{summary[:500]}\n\n"
                f"Re-add the `backporcher` label to retry.",
            )
        await cleanup_task_artifacts(task, db)

        _title = task.get("prompt", "")[:80]
        await notifications.notify_failed(task_id, _title, "coordinator rejected PR (retries exhausted)")
