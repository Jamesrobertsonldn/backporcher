---
title: aiosqlite commit visibility race on back-to-back claims
date: 2026-03-07
tags: [sqlite, concurrency, executor, dependency-chain]
severity: high
---

# Problem

Back-to-back `claim_next_queued()` calls within ~3ms would both succeed, even when the second task depended on the first. The SQL query correctly checks `dep.status = 'completed'`, but aiosqlite's commit from the first claim wasn't visible to the second query yet.

Result: tasks with unmet dependencies were dispatched, causing parallel execution of sequential work (e.g., 5 tasks all modifying App.tsx simultaneously).

# Root Cause

aiosqlite commits are async. When two executor poll iterations fire within milliseconds, the second `claim_next_queued()` reads stale data — the first task is still showing `status='queued'` in its view, so the dependency check passes incorrectly.

# Solution

Post-claim dependency guard in `_task_executor_loop()` (`src/worker.py`):

```python
task = await self.db.claim_next_queued()
if task:
    dep_id = task.get("depends_on_task_id")
    if dep_id:
        dep = await self.db.get_task(dep_id)
        if not dep or dep["status"] != "completed":
            await self.db.update_task(task["id"], status="queued", started_at=None)
            await asyncio.sleep(0.1)
            continue
```

After claiming a task, re-fetch the dependency and verify it's actually completed. If not, put the task back to queued and wait.

# Prevention

- The guard is now permanent in the executor loop
- Any future scheduling logic that depends on task status should do a fresh read after claiming, not rely on the claim query alone
- Consider adding a `SELECT ... FOR UPDATE` equivalent if SQLite ever supports it

# Related

- Commit: `2974b76` (Fix executor race, merge conflicts, and worktree cleanup)
- The `asyncio.sleep(0.1)` prevents tight-loop re-claiming
