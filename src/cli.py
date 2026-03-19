"""CLI entry point: backporcher {status,cancel,cleanup,fleet,repo,worker}."""

import argparse

from .cli_control import cmd_approve, cmd_hold, cmd_pause, cmd_release, cmd_resume
from .cli_repo import cmd_repo_add, cmd_repo_learnings, cmd_repo_list, cmd_repo_verify
from .cli_stats import cmd_stats
from .cli_tasks import cmd_cancel, cmd_cleanup, cmd_fleet, cmd_status


def cmd_worker(args):
    from .worker import run_worker

    run_worker()


def main():
    parser = argparse.ArgumentParser(
        prog="backporcher",
        description="Parallel Claude Code agent dispatcher — GitHub Issues as task queue",
    )
    sub = parser.add_subparsers(dest="command")

    # repo
    repo_parser = sub.add_parser("repo", help="Manage repos")
    repo_sub = repo_parser.add_subparsers(dest="repo_command")

    repo_add = repo_sub.add_parser("add", help="Add a repo")
    repo_add.add_argument("url", help="GitHub repo URL")
    repo_add.add_argument("--branch", default="main", help="Default branch")

    repo_sub.add_parser("list", help="List repos")

    repo_verify = repo_sub.add_parser("verify", help="Set build/test verify command")
    repo_verify.add_argument("name", help="Repo name")
    repo_verify.add_argument("verify_cmd", nargs="*", help="Verify command (omit to clear)")

    repo_learnings = repo_sub.add_parser("learnings", help="Show recorded learnings for a repo")
    repo_learnings.add_argument("name", help="Repo name")

    # fleet
    sub.add_parser("fleet", help="Fleet dashboard — active work overview")

    # status
    status_parser = sub.add_parser("status", help="Check task status")
    status_parser.add_argument("task_id", nargs="?", help="Task ID for detail view")

    # cancel
    cancel_parser = sub.add_parser("cancel", help="Cancel a task")
    cancel_parser.add_argument("task_id", help="Task ID")

    # cleanup
    cleanup_parser = sub.add_parser("cleanup", help="Remove worktrees")
    cleanup_parser.add_argument("task_id", nargs="?", help="Task ID (or all)")

    # approve / hold / release / pause / resume
    approve_parser = sub.add_parser("approve", help="Approve a held task (merge or dispatch)")
    approve_parser.add_argument("task_id", help="Task ID")

    hold_parser = sub.add_parser("hold", help="Set user hold on a task")
    hold_parser.add_argument("task_id", help="Task ID")

    release_parser = sub.add_parser("release", help="Release a user hold")
    release_parser.add_argument("task_id", help="Task ID")

    sub.add_parser("pause", help="Pause the dispatch queue")
    sub.add_parser("resume", help="Resume the dispatch queue")

    # stats
    sub.add_parser("stats", help="Pipeline performance stats")

    # worker
    sub.add_parser("worker", help="Run worker daemon (foreground)")

    args = parser.parse_args()

    if args.command == "repo":
        if args.repo_command == "add":
            cmd_repo_add(args)
        elif args.repo_command == "list":
            cmd_repo_list(args)
        elif args.repo_command == "verify":
            cmd_repo_verify(args)
        elif args.repo_command == "learnings":
            cmd_repo_learnings(args)
        else:
            repo_parser.print_help()
    elif args.command == "fleet":
        cmd_fleet(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "cancel":
        cmd_cancel(args)
    elif args.command == "cleanup":
        cmd_cleanup(args)
    elif args.command == "approve":
        cmd_approve(args)
    elif args.command == "hold":
        cmd_hold(args)
    elif args.command == "release":
        cmd_release(args)
    elif args.command == "pause":
        cmd_pause(args)
    elif args.command == "resume":
        cmd_resume(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "worker":
        cmd_worker(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
