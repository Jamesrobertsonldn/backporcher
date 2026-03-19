"""CLI control commands: approve, hold, release, pause, resume."""

import sys

from .config import load_config
from .db_sync import SyncDatabase


def _get_db() -> SyncDatabase:
    config = load_config()
    db = SyncDatabase(config.db_path)
    db.connect()
    return db


def cmd_approve(args):
    db = _get_db()
    task = db.get_task(int(args.task_id))
    if not task:
        print(f"Task #{args.task_id} not found")
        sys.exit(1)

    hold = task.get("hold")
    if not hold:
        print(f"Task #{args.task_id} has no hold to clear")
        sys.exit(1)

    db.clear_hold(task["id"])
    db.add_log(task["id"], f"Hold '{hold}' cleared via CLI approve")

    if hold == "merge_approval":
        print(f"Approved task #{task['id']} for merge. Will merge on next CI check cycle (~60s).")
    elif hold == "dispatch_approval":
        print(f"Approved task #{task['id']} for dispatch. Will be dispatched on next executor cycle (~5s).")
    else:
        print(f"Cleared hold '{hold}' on task #{task['id']}.")
    db.close()


def cmd_hold(args):
    db = _get_db()
    task = db.get_task(int(args.task_id))
    if not task:
        print(f"Task #{args.task_id} not found")
        sys.exit(1)

    if task["status"] in ("completed", "failed", "cancelled"):
        print(f"Cannot hold task #{args.task_id} (status={task['status']})")
        sys.exit(1)

    db.set_hold(task["id"], "user_hold")
    db.add_log(task["id"], "User hold set via CLI")
    print(f"Held task #{task['id']}. Use 'backporcher approve {task['id']}' to release.")
    db.close()


def cmd_release(args):
    db = _get_db()
    task = db.get_task(int(args.task_id))
    if not task:
        print(f"Task #{args.task_id} not found")
        sys.exit(1)

    if task.get("hold") != "user_hold":
        print(f"Task #{args.task_id} does not have a user hold (hold={task.get('hold')})")
        print(f"Use 'backporcher approve {args.task_id}' to clear any hold type.")
        sys.exit(1)

    db.clear_hold(task["id"])
    db.add_log(task["id"], "User hold released via CLI")
    print(f"Released user hold on task #{task['id']}.")
    db.close()


def cmd_pause(args):
    db = _get_db()
    db.set_queue_paused(True)
    active = db.count_active()
    queued = db.count_queued()
    print(f"Queue paused. {active} task(s) still in-flight (will finish). {queued} queued task(s) on hold.")
    db.close()


def cmd_resume(args):
    db = _get_db()
    db.set_queue_paused(False)
    queued = db.count_queued()
    print(f"Queue resumed. {queued} queued task(s) now eligible for dispatch.")
    db.close()
