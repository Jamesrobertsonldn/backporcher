---
title: Merge conflicts on sequential PRs block the pipeline
date: 2026-03-07
tags: [git, merge-conflicts, ci-monitor, requeue]
severity: high
---

# Problem

In a dependency chain (A -> B -> C), PR for task A merges. Task B's PR was created from pre-merge main, so it has merge conflicts. The `merge_pr()` call fails silently, and the task gets stuck in `ci_passed` status forever — no retry, no re-queue, no cascade.

# Root Cause

Two gaps:
1. `_handle_ci_passed()` didn't check WHY merge failed — it just logged a warning
2. The CI monitor only swept `reviewed` tasks, so `ci_passed` tasks with failed merges were invisible

# Solution

Three changes in `src/worker.py` and `src/github.py`:

**1. Detect conflicts** (`src/github.py`):
```python
async def is_pr_conflicting(repo_full_name, pr_number) -> bool:
    rc, out, _ = await _run_gh("pr", "view", "--repo", repo_full_name,
                                str(pr_number), "--json", "mergeable")
    data = json.loads(out)
    return data.get("mergeable") == "CONFLICTING"
```

**2. Re-queue on conflict** (`_handle_ci_passed()`):
After merge fails, check `is_pr_conflicting()`. If true, close the PR and re-queue the task with all fields cleared (branch_name, pr_url, etc. set to NULL). The next dispatch creates a fresh worktree from latest main.

**3. Sweep stuck tasks** (CI monitor loop):
Added a sweep for `ci_passed` tasks — checks if their PRs are conflicting and re-queues them.

# Prevention

- The conflict detection and re-queue is now automatic
- Sequential dependency chains self-heal: each task gets a fresh worktree from the latest main after its dependency merges
- Monitor logs for "re-queuing" messages to spot chains that are cycling

# Related

- Commit: `2974b76`
- Also required remote branch cleanup (see `stale-branch-cleanup.md`)
