"""CLI stats command: pipeline performance metrics."""

from datetime import datetime, timedelta, timezone

from .config import load_config
from .db_sync import SyncDatabase


def _get_db() -> SyncDatabase:
    config = load_config()
    db = SyncDatabase(config.db_path)
    db.connect()
    return db


def _parse_iso(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _fmt_duration(seconds):
    if seconds is None:
        return "-"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def cmd_stats(args):
    """Print pipeline performance stats."""
    db = _get_db()

    # Total tasks (exclude cancelled)
    all_tasks = db.list_tasks(limit=10000)
    tasks = [t for t in all_tasks if t["status"] != "cancelled"]

    if not tasks:
        print("No tasks yet. Run some work through the pipeline first.")
        db.close()
        return

    completed = [t for t in tasks if t["status"] == "completed"]
    failed = [t for t in tasks if t["status"] == "failed"]
    total = len(tasks)
    n_completed = len(completed)
    n_failed = len(failed)

    # Issue->merge times (created_at -> completed_at for completed tasks)
    merge_times = []
    for t in completed:
        start = _parse_iso(t.get("created_at"))
        end = _parse_iso(t.get("completed_at"))
        if start and end:
            merge_times.append((end - start).total_seconds())

    # Agent runtimes (agent_started_at -> agent_finished_at)
    agent_runtimes = []
    for t in completed:
        start = _parse_iso(t.get("agent_started_at"))
        end = _parse_iso(t.get("agent_finished_at"))
        if start and end:
            agent_runtimes.append((end - start).total_seconds())

    avg_merge = sum(merge_times) / len(merge_times) if merge_times else None
    avg_agent = sum(agent_runtimes) / len(agent_runtimes) if agent_runtimes else None

    # Total retries
    total_retries = sum(t.get("retry_count", 0) for t in tasks)
    retry_rate = (total_retries / total * 100) if total > 0 else 0

    # Model breakdown
    model_counts = {}
    for t in tasks:
        m = t.get("model_used") or t.get("model") or "unknown"
        model_counts[m] = model_counts.get(m, 0) + 1

    # Escalations: tasks where initial_model != model_used
    escalations = 0
    for t in tasks:
        initial = t.get("initial_model")
        used = t.get("model_used")
        if initial and used and initial != used:
            escalations += 1

    # Last 7 days
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    recent = [t for t in tasks if _parse_iso(t.get("created_at")) and _parse_iso(t["created_at"]) >= seven_days_ago]
    recent_completed = [t for t in recent if t["status"] == "completed"]
    recent_failed = [t for t in recent if t["status"] == "failed"]
    recent_merge_times = []
    for t in recent_completed:
        start = _parse_iso(t.get("created_at"))
        end = _parse_iso(t.get("completed_at"))
        if start and end:
            recent_merge_times.append((end - start).total_seconds())
    recent_avg_merge = sum(recent_merge_times) / len(recent_merge_times) if recent_merge_times else None

    # Per-repo breakdown
    repo_stats = {}
    for t in tasks:
        rn = t.get("repo_name", "unknown")
        if rn not in repo_stats:
            repo_stats[rn] = {"total": 0, "failed": 0}
        repo_stats[rn]["total"] += 1
        if t["status"] == "failed":
            repo_stats[rn]["failed"] += 1

    # Print
    pct_completed = (n_completed / total * 100) if total > 0 else 0
    pct_failed = (n_failed / total * 100) if total > 0 else 0

    print("Backporcher Stats")
    print("\u2550" * 39)
    print()
    print("Pipeline")
    print(f"  Total tasks:          {total}")
    print(f"  Completed:            {n_completed} ({pct_completed:.1f}%)")
    print(f"  Failed:               {n_failed} ({pct_failed:.1f}%)")
    print(f"  Avg issue\u2192merge:      {_fmt_duration(avg_merge)}")
    print(f"  Avg agent runtime:    {_fmt_duration(avg_agent)}")
    print(f"  Retry rate:           {retry_rate:.1f}% ({total_retries} retries across {total} tasks)")

    print()
    print("Models")
    for m, count in sorted(model_counts.items(), key=lambda x: -x[1]):
        pct = count / total * 100
        print(f"  {m:<20s} {count} tasks ({pct:.0f}%)")
    print(f"  Escalations:          {escalations}")

    print()
    print("Activity (last 7 days)")
    print(f"  Tasks completed:      {len(recent_completed)}")
    print(f"  Tasks failed:         {len(recent_failed)}")
    print(f"  Avg issue\u2192merge:      {_fmt_duration(recent_avg_merge)}")

    if repo_stats:
        print()
        print("Repos")
        for rn, rs in sorted(repo_stats.items()):
            failed_part = f" ({rs['failed']} failed)" if rs["failed"] else ""
            print(f"  {rn:<20s} {rs['total']} tasks{failed_part}")

    db.close()
