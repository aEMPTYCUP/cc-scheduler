#!/usr/bin/env python3
"""Worktree helper CLI."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import protocol as p


def main():
    parser = argparse.ArgumentParser(description="Git worktree isolation for tasks")
    sub = parser.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create")
    c.add_argument("repo")
    c.add_argument("task_id")
    c.add_argument("--base", default="HEAD")

    c = sub.add_parser("remove")
    c.add_argument("repo")
    c.add_argument("task_id")
    c.add_argument("--force", action="store_true")

    c = sub.add_parser("list")
    c.add_argument("repo")

    args = parser.parse_args()
    if args.cmd == "create":
        p.create_worktree(args.repo, args.task_id, args.base)
    elif args.cmd == "remove":
        p.remove_worktree(args.repo, args.task_id, args.force)
    elif args.cmd == "list":
        for block in p.list_worktrees(args.repo):
            print(block)
            print("---")


if __name__ == "__main__":
    main()
