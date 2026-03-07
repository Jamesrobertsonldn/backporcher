---
title: Coordinator rejection doesn't cascade to dependent tasks
date: 2026-03-07
tags: [coordinator, dependency-chain, cascade]
severity: medium
---

# Problem

When the coordinator rejects a PR, the task is set to `failed` but dependent tasks remain `queued`. They eventually get claimed, dispatched, and fail because their prerequisites weren't met — wasting agent time and API credits.

# Root Cause

`handle_dependency_failure()` was called in `dispatch_task()` (agent failures) and `cmd_cancel()` (CLI cancel), but NOT in the coordinator rejection path in `_coordinator_loop()`.

# Solution

Added cascade call after coordinator rejection (`src/worker.py`):

```python
await self.db.update_task(task_id, status="failed",
    error_message=f"Coordinator rejected: {summary[:500]}")

# Cascade failure to dependent tasks
cascaded = await self.db.handle_dependency_failure(task_id)
if cascaded:
    log.info("Task #%d rejection cascaded to tasks: %s", task_id, cascaded)
```

# Prevention

- Any code path that sets a task to `failed` should also call `handle_dependency_failure()`
- Search for `status="failed"` or `status='failed'` to audit all failure paths
- Consider extracting a `fail_task()` method that always cascades

# Related

- Commit: `b705da1`
- The CI retry failure path (`_handle_ci_failure`) already cascaded correctly
