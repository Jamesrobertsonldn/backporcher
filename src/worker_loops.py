"""Worker loop bodies: re-exports from split modules for backward compatibility."""

from .worker_ci import (
    cleanup_terminal_tasks,
    handle_ci_failure,
    handle_ci_passed,
    monitor_ci,
    process_retry,
)
from .worker_merge import (
    compute_duration,
    merge_approved_task,
)
from .worker_poller import (
    batch_create_tasks,
    create_task_for_issue,
    poll_issues,
    try_claim_and_dispatch,
)
from .worker_review import review_pending_tasks

__all__ = [
    "batch_create_tasks",
    "cleanup_terminal_tasks",
    "compute_duration",
    "create_task_for_issue",
    "handle_ci_failure",
    "handle_ci_passed",
    "merge_approved_task",
    "monitor_ci",
    "poll_issues",
    "process_retry",
    "review_pending_tasks",
    "try_claim_and_dispatch",
]
