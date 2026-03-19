"""CLI repo commands: add, list, verify, learnings, and single-task cleanup."""

import subprocess
import sys
from pathlib import Path

from .config import load_config
from .db_sync import SyncDatabase
from .dispatcher import repo_name_from_url, validate_github_url


def _get_db() -> SyncDatabase:
    config = load_config()
    db = SyncDatabase(config.db_path)
    db.connect()
    return db


def cmd_repo_add(args):
    config = load_config()
    db = _get_db()

    url = args.url.strip().rstrip("/")
    url = validate_github_url(url, config)
    name = repo_name_from_url(url)

    # Check if already exists
    existing = db.get_repo_by_name(name)
    if existing:
        print(f"Repo '{name}' already exists (id={existing['id']})")
        return

    local_path = str(config.repos_dir / name)
    branch = getattr(args, "branch", "main") or "main"
    repo_id = db.add_repo(name, url, local_path, branch)
    print(f"Added repo '{name}' (id={repo_id})")
    db.close()


def cmd_repo_list(args):
    db = _get_db()
    repos = db.list_repos()
    if not repos:
        print("No repos configured. Use: backporcher repo add <url>")
        return
    for r in repos:
        verify = f"  verify: {r['verify_command']}" if r.get("verify_command") else ""
        stack = f"  [{r['stack_info']}]" if r.get("stack_info") else ""
        print(f"  {r['id']:3d}  {r['name']:<20s}  {r['github_url']}{stack}{verify}")
    db.close()


def cmd_repo_verify(args):
    db = _get_db()
    repo = db.get_repo_by_name(args.name)
    if not repo:
        print(f"Repo '{args.name}' not found")
        sys.exit(1)

    command = " ".join(args.verify_cmd) if args.verify_cmd else None
    db.update_repo(repo["id"], verify_command=command)

    if command:
        print(f"Set verify command for '{args.name}': {command}")
    else:
        print(f"Cleared verify command for '{args.name}'")
    db.close()


def cmd_repo_learnings(args):
    db = _get_db()
    repo = db.get_repo_by_name(args.name)
    if not repo:
        print(f"Repo '{args.name}' not found")
        sys.exit(1)

    learnings = db.get_learnings(repo["id"], limit=20)
    if not learnings:
        print(f"No learnings recorded for '{args.name}'")
        db.close()
        return

    print(f"Learnings for '{args.name}' ({len(learnings)} entries):")
    for entry in learnings:
        icon = {
            "success": "+",
            "agent_failure": "!",
            "verify_failure": "!",
            "ci_failure": "!",
            "coordinator_rejection": "!",
        }.get(entry["learning_type"], "-")
        task_ref = f" (task #{entry['task_id']})" if entry.get("task_id") else ""
        ts = entry["created_at"].split("T")[0] if "T" in entry["created_at"] else entry["created_at"][:10]
        print(f"  [{icon}] {ts}{task_ref}  {entry['content']}")
    db.close()


def cleanup_single_task(task: dict, db: SyncDatabase):
    """Clean up worktree and remote branch for a single task. Returns (worktree_removed, branch_deleted)."""
    wt_removed = False
    br_deleted = False
    repo = db.get_repo_by_name(task["repo_name"])
    if not repo:
        return wt_removed, br_deleted

    repo_path = repo["local_path"]

    # Remove worktree
    wt = task.get("worktree_path")
    if wt and Path(wt).exists():
        rc = subprocess.run(
            ["git", "worktree", "remove", "--force", wt],
            cwd=repo_path,
            capture_output=True,
        )
        if rc.returncode == 0:
            wt_removed = True
        else:
            # Force-remove directory if git command failed
            import shutil

            shutil.rmtree(wt, ignore_errors=True)
            wt_removed = Path(wt).exists() is False

    # Prune stale worktree refs
    if wt_removed:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=repo_path,
            capture_output=True,
        )

    # Delete remote branch
    branch = task.get("branch_name")
    if branch:
        rc = subprocess.run(
            ["git", "push", "origin", "--delete", branch],
            cwd=repo_path,
            capture_output=True,
            timeout=30,
        )
        if rc.returncode == 0:
            br_deleted = True

    # Clear paths in DB
    if wt_removed or br_deleted:
        db.update_task(
            task["id"],
            worktree_path=None,
            branch_name=None,
        )

    return wt_removed, br_deleted
